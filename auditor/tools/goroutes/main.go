// Command goroutes extracts an HTTP route table from Go source using go/ast.
//
// This is deliberately not a model reading files. Enumerating every registered
// route is the part of contract auditing a parser does perfectly and an LLM does
// approximately, so it is done here, once, exactly.
//
// It recognises two registration styles:
//
//	stdlib  mux.HandleFunc("POST /v1/payouts", CreatePayout)   // Go 1.22 patterns
//	        mux.Handle("/v1/payouts", h)
//	router  r.POST("/payouts", CreatePayout)                   // gin, echo, chi
//	        v1 := r.Group("/v1")                               // prefixes resolved
//
// It also collects swag annotations from handler doc comments, because the
// annotation is a code-side claim about the contract and belongs with the route
// it describes.
//
// Output is JSON on stdout.
package main

import (
	"encoding/json"
	"flag"
	"fmt"
	"go/ast"
	"go/parser"
	"go/token"
	"io/fs"
	"os"
	"path/filepath"
	"regexp"
	"sort"
	"strconv"
	"strings"
)

// Route is one registered HTTP endpoint found in the source.
type Route struct {
	Method     string      `json:"method"`
	Path       string      `json:"path"`
	Handler    string      `json:"handler"`
	File       string      `json:"file"`
	Line       int         `json:"line"`
	Style      string      `json:"style"`
	Annotation *Annotation `json:"annotation"`
}

// Annotation is the swag contract claim written above a handler.
type Annotation struct {
	Path     string   `json:"path"`
	Method   string   `json:"method"`
	Summary  string   `json:"summary"`
	Success  []Status `json:"success"`
	Failure  []Status `json:"failure"`
	Params   []Param  `json:"params"`
	Security []string `json:"security"`
	File     string   `json:"file"`
	Line     int      `json:"line"`
}

// Status is one documented response.
type Status struct {
	Code   string `json:"code"`
	Schema string `json:"schema"`
	Typed  bool   `json:"typed"`
}

// Param is one documented request parameter.
type Param struct {
	Name     string `json:"name"`
	In       string `json:"in"`
	Type     string `json:"type"`
	Required bool   `json:"required"`
}

type handlerDoc struct {
	name string
	doc  string
	file string
	line int
}

var (
	httpMethods = map[string]bool{
		"GET": true, "POST": true, "PUT": true, "PATCH": true,
		"DELETE": true, "HEAD": true, "OPTIONS": true,
	}
	// ":id" (gin, echo) and "*wildcard" (chi) normalised to OpenAPI "{id}".
	colonParam = regexp.MustCompile(`:([A-Za-z_][A-Za-z0-9_]*)`)
	starParam  = regexp.MustCompile(`\*([A-Za-z_][A-Za-z0-9_]*)`)
	spaces     = regexp.MustCompile(`\s+`)
)

func main() {
	dir := flag.String("dir", ".", "directory of Go source to scan")
	stripPrefix := flag.String("strip-prefix", "", "path prefix to strip from every route, e.g. /v1")
	flag.Parse()

	fset := token.NewFileSet()
	files, err := parseDir(fset, *dir)
	if err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}

	docs := collectHandlerDocs(fset, files, *dir)
	routes := collectRoutes(fset, files, *dir)
	structs := collectStructs(fset, files, *dir)
	facts := collectHandlerFacts(fset, files, *dir)

	// Attach each handler's annotation to the route that registers it.
	claimed := map[string]bool{}
	for i := range routes {
		if doc, ok := docs[routes[i].Handler]; ok {
			if ann := parseAnnotation(doc); ann != nil {
				routes[i].Annotation = ann
				claimed[routes[i].Handler] = true
			}
		}
		routes[i].Path = normalisePath(routes[i].Path, *stripPrefix)
	}

	// An annotation with no route registering it is a contract claim with no
	// implementation behind it - reported, not silently dropped.
	orphaned := []Annotation{}
	for name, doc := range docs {
		if claimed[name] {
			continue
		}
		if ann := parseAnnotation(doc); ann != nil {
			orphaned = append(orphaned, *ann)
		}
	}
	sort.Slice(orphaned, func(i, j int) bool { return orphaned[i].Path < orphaned[j].Path })

	sort.Slice(routes, func(i, j int) bool {
		if routes[i].Path != routes[j].Path {
			return routes[i].Path < routes[j].Path
		}
		return routes[i].Method < routes[j].Method
	})

	out := map[string]any{
		"dir":                       *dir,
		"strip_prefix":              *stripPrefix,
		"routes":                    routes,
		"annotations_unrouted":      orphaned,
		"structs":                   structs,
		"handlers":                  facts,
		"route_count":               len(routes),
		"routes_without_annotation": countMissing(routes),
	}
	enc := json.NewEncoder(os.Stdout)
	enc.SetIndent("", "  ")
	if err := enc.Encode(out); err != nil {
		fmt.Fprintln(os.Stderr, err)
		os.Exit(1)
	}
}

func countMissing(routes []Route) int {
	n := 0
	for _, r := range routes {
		if r.Annotation == nil {
			n++
		}
	}
	return n
}

func parseDir(fset *token.FileSet, dir string) (map[string]*ast.File, error) {
	files := map[string]*ast.File{}
	err := filepath.WalkDir(dir, func(path string, d fs.DirEntry, err error) error {
		if err != nil {
			return err
		}
		if d.IsDir() {
			if name := d.Name(); name == "vendor" || strings.HasPrefix(name, ".") && name != "." {
				return filepath.SkipDir
			}
			return nil
		}
		if !strings.HasSuffix(path, ".go") || strings.HasSuffix(path, "_test.go") {
			return nil
		}
		f, perr := parser.ParseFile(fset, path, nil, parser.ParseComments)
		if perr != nil {
			return fmt.Errorf("parse %s: %w", path, perr)
		}
		files[path] = f
		return nil
	})
	return files, err
}

func collectHandlerDocs(fset *token.FileSet, files map[string]*ast.File, root string) map[string]handlerDoc {
	docs := map[string]handlerDoc{}
	for _, path := range sortedPaths(files) {
		file := files[path]
		for _, decl := range file.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Doc == nil {
				continue
			}
			if _, clash := docs[fn.Name.Name]; clash {
				continue // first in sorted order wins, deterministically
			}
			docs[fn.Name.Name] = handlerDoc{
				name: fn.Name.Name,
				doc:  fn.Doc.Text(),
				file: rel(root, path),
				line: fset.Position(fn.Pos()).Line,
			}
		}
	}
	return docs
}

func collectRoutes(fset *token.FileSet, files map[string]*ast.File, root string) []Route {
	// Initialised, not nil: a nil slice marshals to JSON `null`, and a consumer
	// that expects a list then fails with a type error instead of reporting that
	// no routes were found. Same trap as the orphaned-annotations slice.
	routes := []Route{}

	paths := make([]string, 0, len(files))
	for p := range files {
		paths = append(paths, p)
	}
	sort.Strings(paths)

	for _, path := range paths {
		// Group prefixes are per file; ast.Inspect walks in source order, so an
		// assignment is always seen before the calls that use it.
		prefixes := map[string]string{}

		ast.Inspect(files[path], func(n ast.Node) bool {
			if assign, ok := n.(*ast.AssignStmt); ok {
				recordGroup(assign, prefixes)
				return true
			}

			call, ok := n.(*ast.CallExpr)
			if !ok {
				return true
			}
			sel, ok := call.Fun.(*ast.SelectorExpr)
			if !ok {
				return true
			}

			name := sel.Sel.Name
			pos := fset.Position(call.Pos())
			prefix := prefixes[receiverName(sel.X)]

			switch {
			// Router style. Two structural guards keep this from swallowing
			// unrelated calls that merely share a method name - net/http's
			// Header.Get and Values.Get being the ones that actually bite, and
			// both take a single argument. chi's Get/Post spelling is accepted
			// alongside gin's GET/POST.
			//
			// The empty path is deliberate, not an oversight: gin registers a
			// group's own root as group.GET("", handler), where the path comes
			// entirely from the group prefix. Requiring a leading slash drops
			// those, and they are real routes.
			case httpMethods[strings.ToUpper(name)] && len(call.Args) >= 2:
				lit, ok := stringArg(call.Args[0])
				if ok && (lit == "" || strings.HasPrefix(lit, "/")) {
					routes = append(routes, Route{
						Method:  strings.ToUpper(name),
						Path:    prefix + lit,
						Handler: handlerName(call.Args),
						File:    rel(root, path), Line: pos.Line, Style: "router",
					})
				}

			case (name == "HandleFunc" || name == "Handle") && len(call.Args) >= 1:
				if lit, ok := stringArg(call.Args[0]); ok {
					method, routePath := splitPattern(lit)
					routes = append(routes, Route{
						Method:  method,
						Path:    prefix + routePath,
						Handler: handlerName(call.Args),
						File:    rel(root, path), Line: pos.Line, Style: "stdlib",
					})
				}
			}
			return true
		})
	}
	return routes
}

// recordGroup tracks `v1 := r.Group("/v1")` so nested prefixes resolve.
func recordGroup(assign *ast.AssignStmt, prefixes map[string]string) {
	if len(assign.Lhs) != 1 || len(assign.Rhs) != 1 {
		return
	}
	ident, ok := assign.Lhs[0].(*ast.Ident)
	if !ok {
		return
	}
	call, ok := assign.Rhs[0].(*ast.CallExpr)
	if !ok {
		return
	}
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok || (sel.Sel.Name != "Group" && sel.Sel.Name != "Route") || len(call.Args) == 0 {
		return
	}
	lit, ok := stringArg(call.Args[0])
	if !ok {
		return
	}
	prefixes[ident.Name] = prefixes[receiverName(sel.X)] + lit
}

// splitPattern handles the Go 1.22 "METHOD /path" ServeMux pattern. A pattern
// with no method matches every method, reported as ANY rather than guessed.
func splitPattern(pattern string) (string, string) {
	pattern = strings.TrimSpace(pattern)
	if i := strings.Index(pattern, " "); i > 0 {
		method := strings.ToUpper(strings.TrimSpace(pattern[:i]))
		if httpMethods[method] {
			return method, strings.TrimSpace(pattern[i+1:])
		}
	}
	// A host may precede the path in a ServeMux pattern; only the path matters.
	if i := strings.Index(pattern, "/"); i > 0 {
		return "ANY", pattern[i:]
	}
	return "ANY", pattern
}

func receiverName(expr ast.Expr) string {
	if ident, ok := expr.(*ast.Ident); ok {
		return ident.Name
	}
	return ""
}

func stringArg(expr ast.Expr) (string, bool) {
	lit, ok := expr.(*ast.BasicLit)
	if !ok || lit.Kind != token.STRING {
		return "", false
	}
	value, err := strconv.Unquote(lit.Value)
	if err != nil {
		return "", false
	}
	return value, true
}

// handlerName reports the handler as written. A handler built inline by a
// wrapper is reported as its expression rather than resolved, so the output
// never implies more certainty than the AST supports.
func handlerName(args []ast.Expr) string {
	if len(args) < 2 {
		return ""
	}
	switch h := args[len(args)-1].(type) {
	case *ast.Ident:
		return h.Name
	case *ast.SelectorExpr:
		return h.Sel.Name
	case *ast.CallExpr:
		if sel, ok := h.Fun.(*ast.SelectorExpr); ok {
			return sel.Sel.Name + "()"
		}
		if ident, ok := h.Fun.(*ast.Ident); ok {
			return ident.Name + "()"
		}
	case *ast.FuncLit:
		return "<inline>"
	}
	return "<expr>"
}

func normalisePath(path, stripPrefix string) string {
	path = colonParam.ReplaceAllString(path, "{$1}")
	path = starParam.ReplaceAllString(path, "{$1}")
	if stripPrefix != "" && strings.HasPrefix(path, stripPrefix) {
		if trimmed := strings.TrimPrefix(path, stripPrefix); strings.HasPrefix(trimmed, "/") {
			path = trimmed
		}
	}
	if path == "" {
		path = "/"
	}
	if len(path) > 1 {
		path = strings.TrimSuffix(path, "/")
	}
	return path
}

func rel(root, path string) string {
	if r, err := filepath.Rel(root, path); err == nil {
		return r
	}
	return path
}

func parseAnnotation(doc handlerDoc) *Annotation {
	ann := &Annotation{File: doc.file, Line: doc.line}
	found := false

	for _, raw := range strings.Split(doc.doc, "\n") {
		line := strings.TrimSpace(raw)
		if !strings.HasPrefix(line, "@") {
			continue
		}
		fields := spaces.Split(line, 2)
		tag := strings.ToLower(fields[0])
		rest := ""
		if len(fields) > 1 {
			rest = strings.TrimSpace(fields[1])
		}

		switch tag {
		case "@router":
			// "/payouts/{id} [get]"
			parts := spaces.Split(rest, 2)
			ann.Path = parts[0]
			if len(parts) > 1 {
				ann.Method = strings.ToLower(strings.Trim(parts[1], "[]"))
			}
			found = true
		case "@summary":
			ann.Summary = rest
		case "@success":
			ann.Success = append(ann.Success, parseStatus(rest))
		case "@failure":
			ann.Failure = append(ann.Failure, parseStatus(rest))
		case "@param":
			if p, ok := parseParam(rest); ok {
				ann.Params = append(ann.Params, p)
			}
		case "@security":
			for _, s := range strings.Split(rest, "&&") {
				if s = strings.TrimSpace(s); s != "" {
					ann.Security = append(ann.Security, s)
				}
			}
		}
	}

	if !found {
		return nil
	}
	return ann
}

// parseStatus reads "200  {object}  PayoutResponse". A schema of
// map[string]interface{} documents nothing about the body, so it is recorded as
// untyped - that distinction is a finding in its own right.
func parseStatus(rest string) Status {
	fields := spaces.Split(rest, -1)
	s := Status{}
	if len(fields) > 0 {
		s.Code = fields[0]
	}
	if len(fields) > 2 {
		s.Schema = fields[2]
	}
	s.Typed = s.Schema != "" &&
		!strings.HasPrefix(s.Schema, "map[") &&
		s.Schema != "interface{}" &&
		s.Schema != "any"
	return s
}

// parseParam reads `name  in  type  required  "description"`.
func parseParam(rest string) (Param, bool) {
	if i := strings.Index(rest, `"`); i >= 0 {
		rest = rest[:i]
	}
	fields := spaces.Split(strings.TrimSpace(rest), -1)
	if len(fields) < 4 {
		return Param{}, false
	}
	return Param{
		Name:     fields[0],
		In:       strings.ToLower(fields[1]),
		Type:     fields[2],
		Required: strings.EqualFold(fields[3], "true"),
	}, true
}
