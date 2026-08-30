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
	Method string `json:"method"`
	Path   string `json:"path"`
	// Middleware names the guards standing in front of this route, in the same
	// shape the TypeScript extractor emits. Without it contract-middleware
	// could not be used on a Go project at all, so a repository registering its
	// public API and its dashboard in one binary had no way to tell the auditor
	// which was which - and every dashboard route read as an undocumented part
	// of the published contract.
	Middleware []string    `json:"middleware"`
	Handler    string      `json:"handler"`
	File       string      `json:"file"`
	Line       int         `json:"line"`
	Style      string      `json:"style"`
	Annotation *Annotation `json:"annotation"`
}

// scoped is middleware attached to a router variable by r.Use(...), with the
// line it was attached on. Order matters and is not cosmetic: chi and gin both
// apply Use() only to what is registered after it, so a webhook registered
// above the line is open, and calling it guarded would hide exactly the thing
// an auth rule exists to find.
type scoped struct {
	name string
	line int
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
	routes, unresolved := collectRoutes(fset, files, *dir)
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
		"unresolved":                unresolved,
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

// echoStyle reports whether a file drives echo, which is the one supported
// router that puts the handler before its middleware:
//
//	echo:      e.GET("/x", handler, mw...)
//	gin, chi:  r.GET("/x", mw..., handler)
//
// Guessing wrong swaps a guard for the handler, so this is read from the file's
// own imports rather than inferred from the shape of the call.
func echoStyle(file *ast.File) bool {
	for _, imported := range file.Imports {
		if imported.Path != nil && strings.Contains(imported.Path.Value, "labstack/echo") {
			return true
		}
	}
	return false
}

// argNames is every argument that names something, which is what a guard looks
// like. A function literal is a handler written inline, never a named guard.
func argNames(args []ast.Expr) []string {
	out := []string{}
	for _, arg := range args {
		if name := exprName(arg); name != "" {
			out = append(out, name)
		}
	}
	return out
}

func exprName(expr ast.Expr) string {
	switch node := expr.(type) {
	case *ast.Ident:
		return node.Name
	case *ast.SelectorExpr:
		return node.Sel.Name
	case *ast.CallExpr:
		// A guard built by a factory keeps its parentheses, so a reader can tell
		// RequireAuth() from RequireAuth in the route table.
		switch fn := node.Fun.(type) {
		case *ast.Ident:
			return fn.Name + "()"
		case *ast.SelectorExpr:
			return fn.Sel.Name + "()"
		}
	}
	return ""
}

// withChain is the guards attached by chi's .With(...), which may be chained.
func withChain(expr ast.Expr) []string {
	out := []string{}
	for {
		call, ok := expr.(*ast.CallExpr)
		if !ok {
			return out
		}
		sel, ok := call.Fun.(*ast.SelectorExpr)
		if !ok || sel.Sel.Name != "With" {
			return out
		}
		out = append(argNames(call.Args), out...)
		expr = sel.X
	}
}

// Unresolved is a route registration whose path this parser could not read,
// because the program computes it at runtime:
//
//	g.GET("/"+ms.Name()+"/status", fw.Status)
//
// vikunja registers eighteen migration endpoints that way, one per migrator,
// with the name coming from an interface method. Nothing static can resolve it.
//
// What matters is that they are counted rather than ignored. A documented
// endpoint that no extracted route matches is normally drift; where the parser
// knows it failed to read some registrations, it is at least as likely to be
// one of those, and reporting it as certainly missing would be a false claim
// about the code.
type Unresolved struct {
	File string `json:"file"`
	Line int    `json:"line"`
}

func collectRoutes(fset *token.FileSet, files map[string]*ast.File, root string) ([]Route, []Unresolved) {
	// Initialised, not nil: a nil slice marshals to JSON `null`, and a consumer
	// that expects a list then fails with a type error instead of reporting that
	// no routes were found. Same trap as the orphaned-annotations slice.
	routes := []Route{}
	unresolved := []Unresolved{}

	paths := make([]string, 0, len(files))
	for p := range files {
		paths = append(paths, p)
	}
	sort.Strings(paths)

	// Routers handed to a function, by callee name, across the whole project.
	// Not per file: the function a router is passed to usually lives in another
	// package - vikunja registers its migration endpoints through a
	// RegisterRoutes method three directories away - so a per-file map never
	// connects the call to the declaration.
	bindings := map[string]string{}
	bindingGuards := map[string][]string{}

	// walkFile runs the collector over one file, or over one function inside it
	// when `only` is set and its first parameter is seeded with `seed`.
	walkFile := func(path string, only *ast.FuncDecl, seedName, seedPrefix string, seedGuards []string) {
		// Group prefixes are per file; ast.Inspect walks in source order, so an
		// assignment is always seen before the calls that use it.
		prefixes := map[string]string{}
		// Guards a router variable carries: inherited from the group it was
		// opened in, plus whatever r.Use() has attached to it so far.
		inherited := map[string][]string{}
		attached := map[string][]scoped{}
		echo := echoStyle(files[path])

		guardsFor := func(receiver string, line int) []string {
			out := append([]string{}, inherited[receiver]...)
			for _, use := range attached[receiver] {
				if use.line < line {
					out = append(out, use.name)
				}
			}
			return out
		}

		var visit func(n ast.Node) bool
		visit = func(n ast.Node) bool {
			if assign, ok := n.(*ast.AssignStmt); ok {
				recordGroup(assign, prefixes)
				// gin and echo open a group with its guards in the same call:
				// v1 := r.Group("/v1", RequireAPIKey). The prefix half of that
				// is recordGroup's job; this is the other half.
				if len(assign.Lhs) == 1 && len(assign.Rhs) == 1 {
					if ident, isIdent := assign.Lhs[0].(*ast.Ident); isIdent {
						if call, isCall := assign.Rhs[0].(*ast.CallExpr); isCall {
							if sel, isSel := call.Fun.(*ast.SelectorExpr); isSel &&
								(sel.Sel.Name == "Group" || sel.Sel.Name == "Route") &&
								len(call.Args) > 1 {
								parent := receiverName(sel.X)
								group := append([]string{}, guardsFor(parent, fset.Position(call.Pos()).Line)...)
								inherited[ident.Name] = append(group, argNames(call.Args[1:])...)
							}
						}
					}
				}
				return true
			}

			call, ok := n.(*ast.CallExpr)
			if !ok {
				return true
			}
			sel, isSel := call.Fun.(*ast.SelectorExpr)
			if !isSel {
				// A router passed to a plain function: mountRoutes(v1, h).
				if fn, isIdent := call.Fun.(*ast.Ident); isIdent {
					recordBinding(fn.Name, call, prefixes, guardsFor, bindings, bindingGuards, fset.Position(call.Pos()).Line)
				}
				return true
			}

			name := sel.Sel.Name
			pos := fset.Position(call.Pos())
			receiver := receiverName(sel.X)
			prefix := prefixes[receiver]

			if !httpMethods[strings.ToUpper(name)] && name != "Use" && name != "With" &&
				name != "Group" && name != "Route" && name != "Handle" && name != "HandleFunc" {
				recordBinding(name, call, prefixes, guardsFor, bindings, bindingGuards, pos.Line)
			}

			switch {
			// r.Use(mw) guards everything registered on r after this line.
			case name == "Use" && len(call.Args) >= 1:
				for _, guard := range argNames(call.Args) {
					attached[receiver] = append(attached[receiver], scoped{guard, pos.Line})
				}
				return true
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
				// A path without a leading slash is only accepted on a receiver
				// already known to be a router. Without that condition an
				// outbound client call - http.Post("https://...", ...) - reads
				// as a route registration, which is why the slash was required
				// in the first place.
				_, known := prefixes[receiver]
				if ok && lit != "" && !strings.HasPrefix(lit, "/") {
					ok = known && !strings.Contains(lit, "://") && !strings.Contains(lit, " ")
				}
				// A router we are tracking, registering something at a path we
				// cannot read. Recorded so the rules know the surface is
				// understated here rather than complete.
				if !ok && known {
					unresolved = append(unresolved, Unresolved{
						File: rel(root, path), Line: pos.Line})
				}
				if ok {
					// Everything between the path and the handler is a guard.
					// Which end the handler sits at is the framework's choice,
					// which is why echoStyle read it from the imports.
					var extra []ast.Expr
					if echo {
						extra = call.Args[min(2, len(call.Args)):]
					} else {
						extra = call.Args[1 : len(call.Args)-1]
					}
					guards := append(guardsFor(receiver, pos.Line), withChain(sel.X)...)
					routes = append(routes, Route{
						Method:     strings.ToUpper(name),
						Path:       joinPath(prefix, lit),
						Middleware: append(guards, argNames(extra)...),
						Handler:    handlerName(call.Args, echo),
						File:       rel(root, path), Line: pos.Line, Style: "router",
					})
				}

			// chi's closure form, which is how chi's own documentation writes
			// nesting:
			//
			//	r.Route("/v1", func(r chi.Router) { r.Get("/x", h) })
			//
			// The prefix binds to the closure's parameter rather than to a
			// variable on the left of an assignment, so the assignment tracker
			// above never saw it and every route in a project written this way
			// came out at its bare path. One real repository's 171 routes
			// shared four distinct paths between them and matched none of its
			// 68 documented operations.
			case (name == "Route" || name == "Group") && len(call.Args) >= 1:
				inner, ok := prefix, true
				guards := guardsFor(receiver, pos.Line)
				if name == "Route" {
					var lit string
					lit, ok = stringArg(call.Args[0])
					inner = prefix + lit
					guards = append(guards, argNames(call.Args[1:len(call.Args)-1])...)
				}
				if ok {
					if fn, isFunc := call.Args[len(call.Args)-1].(*ast.FuncLit); isFunc {
						params := fn.Type.Params
						if params != nil && len(params.List) == 1 && len(params.List[0].Names) == 1 {
							param := params.List[0].Names[0].Name
							prefixes[param] = inner
							inherited[param] = guards
						}
					}
				}

			case (name == "HandleFunc" || name == "Handle") && len(call.Args) >= 1:
				if lit, ok := stringArg(call.Args[0]); ok {
					method, routePath := splitPattern(lit)
					routes = append(routes, Route{
						Method:     method,
						Path:       prefix + routePath,
						Middleware: guardsFor(receiver, pos.Line),
						Handler:    handlerName(call.Args, echo),
						File:       rel(root, path), Line: pos.Line, Style: "stdlib",
					})
				}
			}
			return true
		}
		if only == nil {
			ast.Inspect(files[path], visit)
			return
		}
		prefixes[seedName] = seedPrefix
		inherited[seedName] = seedGuards
		ast.Inspect(only.Body, visit)
	}

	// Pass 1: every file on its own, which is also what records the bindings.
	for _, path := range paths {
		walkFile(path, nil, "", "", nil)
	}

	// Pass 2: the functions a router was handed to, wherever they are declared.
	// Seeding the parameter with the caller's prefix and walking the body again
	// puts those routes at the path they actually serve; the bare-path copies
	// from pass 1 are dropped below.
	for _, path := range paths {
		for _, decl := range files[path].Decls {
			fn, isFunc := decl.(*ast.FuncDecl)
			if !isFunc || fn.Body == nil {
				continue
			}
			prefix, bound := bindings[fn.Name.Name]
			if !bound || fn.Type.Params == nil || len(fn.Type.Params.List) == 0 {
				continue
			}
			names := fn.Type.Params.List[0].Names
			if len(names) != 1 {
				continue
			}
			walkFile(path, fn, names[0].Name, prefix, bindingGuards[fn.Name.Name])
		}
	}
	return dropShadowed(routes), unresolved
}

// joinPath puts one path segment after another, tolerating a missing slash.
//
// gin accepts a group and a route written without one - Group("api") then
// POST("createApi", h) - and it is a common house style. Requiring the slash
// dropped every route in such a project: one real repository extracted a single
// route out of 258 documented operations.
// recordBinding notes that a router variable was handed to a function:
//
//	a.mountEventIntakeRoutes(v1Router, handler)
//
// which is the most common way a Go service splits its routing up. The
// parameter inside that function carries the caller's prefix, and nothing in
// the callee's own text says so. Without following it those routes come out at
// their bare path and read as documented but never implemented - five findings
// on one real repository, every one of them false.
func recordBinding(callee string, call *ast.CallExpr, prefixes map[string]string,
	guardsFor func(string, int) []string, bindings map[string]string,
	bindingGuards map[string][]string, line int) {
	if len(call.Args) == 0 {
		return
	}
	arg, isIdent := call.Args[0].(*ast.Ident)
	if !isIdent {
		return
	}
	prefix, tracked := prefixes[arg.Name]
	if !tracked {
		return
	}
	// First call site wins, deterministically. A router mounted twice under two
	// prefixes cannot be resolved to one path, and guessing would be worse than
	// reporting the first.
	if _, seen := bindings[callee]; seen {
		return
	}
	bindings[callee] = prefix
	// Everything guarding the router at the call site, r.Use() included. Taking
	// only what it inherited missed the guard applied on the line above the
	// call, and the routes inside then read as registered with no auth at all -
	// a critical finding, and wrong.
	bindingGuards[callee] = guardsFor(arg.Name, line)
}

// dropShadowed removes a route collected at a bare path when the same
// registration was also collected under a prefix. One registration, seen twice
// because the function holding it was walked once on its own and once as a
// mount target; the longer path is the one it serves.
func dropShadowed(routes []Route) []Route {
	best := map[string]string{}
	for _, route := range routes {
		key := fmt.Sprintf("%s:%d:%s", route.File, route.Line, route.Method)
		if len(route.Path) > len(best[key]) {
			best[key] = route.Path
		}
	}
	out := routes[:0]
	for _, route := range routes {
		key := fmt.Sprintf("%s:%d:%s", route.File, route.Line, route.Method)
		if route.Path == best[key] {
			out = append(out, route)
		}
	}
	return out
}

func joinPath(prefix, segment string) string {
	if segment == "" {
		return prefix
	}
	if !strings.HasPrefix(segment, "/") {
		segment = "/" + segment
	}
	return prefix + segment
}

// groupCall finds the Group/Route call on the right of an assignment, looking
// through the middleware chained onto it:
//
//	apiRouter := Router.Group("api").Use(Auth())
func groupCall(expr ast.Expr) *ast.CallExpr {
	for {
		call, ok := expr.(*ast.CallExpr)
		if !ok {
			return nil
		}
		sel, ok := call.Fun.(*ast.SelectorExpr)
		if !ok {
			return nil
		}
		if (sel.Sel.Name == "Group" || sel.Sel.Name == "Route") && len(call.Args) > 0 {
			return call
		}
		if sel.Sel.Name != "Use" && sel.Sel.Name != "With" {
			return nil
		}
		expr = sel.X
	}
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
	call := groupCall(assign.Rhs[0])
	if call == nil {
		return
	}
	sel := call.Fun.(*ast.SelectorExpr)
	lit, ok := stringArg(call.Args[0])
	if !ok {
		return
	}
	prefixes[ident.Name] = joinPath(prefixes[receiverName(sel.X)], lit)
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

// receiverName is the name an expression ultimately refers to, looking through
// chi's per-route middleware chain:
//
//	projectRouter.With(RequireEnabledProject()).Put("/", handler.UpdateProject)
//
// .With() returns a router, so the receiver of .Put is a call rather than a
// name and the prefix bound to projectRouter was lost. What that dropped was
// precisely the guarded routes - 48 of one repository's 68 documented
// operations - which are the ones an audit most needs to see.
func receiverName(expr ast.Expr) string {
	for {
		switch node := expr.(type) {
		case *ast.Ident:
			return node.Name
		case *ast.CallExpr:
			sel, ok := node.Fun.(*ast.SelectorExpr)
			if !ok || sel.Sel.Name != "With" {
				return ""
			}
			expr = sel.X
		default:
			return ""
		}
	}
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
func handlerName(args []ast.Expr, echo bool) string {
	if len(args) < 2 {
		return ""
	}
	// echo takes the handler first and its middleware after; every other
	// supported router takes the handler last.
	pick := len(args) - 1
	if echo {
		pick = 1
	}
	switch h := args[pick].(type) {
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
