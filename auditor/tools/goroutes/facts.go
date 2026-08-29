package main

// Struct shapes and handler-body facts.
//
// The route table says which endpoints exist. These say what they actually do:
// which JSON fields a response struct carries, which status codes a handler can
// emit, which query parameters and headers it reads, which headers it sets.
//
// Everything here is read off the AST rather than inferred, so a rule built on
// it can cite a file and line. Where the AST does not settle a question - a
// status code held in a variable, a header name built by concatenation - the
// fact is simply absent rather than guessed. A missing fact costs recall; a
// wrong fact costs precision, and precision is the harder one to win back.

import (
	"go/ast"
	"go/token"
	"go/types"
	"reflect"
	"sort"
	"strconv"
	"strings"
)

// StructDef is a Go struct and the JSON shape it serialises to.
type StructDef struct {
	Name   string  `json:"name"`
	Fields []Field `json:"fields"`
	File   string  `json:"file"`
	Line   int     `json:"line"`
	// Ambiguous marks a name declared in more than one package. The AST alone
	// cannot say which declaration a given route refers to, so rules decline to
	// judge it rather than cite a file and line that may belong to the wrong one.
	Ambiguous bool `json:"ambiguous"`
}

// Field is one struct field as it appears on the wire.
type Field struct {
	Name      string `json:"name"`
	JSONName  string `json:"json_name"`
	GoType    string `json:"go_type"`
	JSONType  string `json:"json_type"`
	Omitempty bool   `json:"omitempty"`
	Skipped   bool   `json:"skipped"`
	Line      int    `json:"line"`
}

// HandlerFacts is what a handler body demonstrably does.
type HandlerFacts struct {
	Name        string   `json:"name"`
	Statuses    []int    `json:"statuses"`
	SuccessCode int      `json:"success_code"`
	QueryParams []string `json:"query_params"`
	HeadersRead []string `json:"headers_read"`
	HeadersSet  []string `json:"headers_set"`
	File        string   `json:"file"`
	Line        int      `json:"line"`
	// EndLine closes the function body, so a caller can slice the exact source
	// of one handler out of its file instead of sending the whole package to a
	// model and hoping it reads the right function.
	EndLine   int  `json:"end_line"`
	Ambiguous bool `json:"ambiguous"`
}

// statusNames maps the net/http status constants to their codes. Only the
// constant form is recognised; a numeric literal passed as a status is not
// assumed to be one.
var statusNames = map[string]int{
	"StatusOK": 200, "StatusCreated": 201, "StatusAccepted": 202,
	"StatusNoContent": 204, "StatusMovedPermanently": 301, "StatusFound": 302,
	"StatusNotModified": 304, "StatusBadRequest": 400, "StatusUnauthorized": 401,
	"StatusPaymentRequired": 402, "StatusForbidden": 403, "StatusNotFound": 404,
	"StatusMethodNotAllowed": 405, "StatusConflict": 409, "StatusGone": 410,
	"StatusPreconditionFailed": 412, "StatusUnsupportedMediaType": 415,
	"StatusUnprocessableEntity": 422, "StatusTooManyRequests": 429,
	"StatusInternalServerError": 500, "StatusNotImplemented": 501,
	"StatusBadGateway": 502, "StatusServiceUnavailable": 503,
	"StatusGatewayTimeout": 504,
}

// goToJSONType maps Go types to the JSON Schema type they serialise as.
// Anything unrecognised yields "" rather than a guess.
func goToJSONType(goType string) string {
	t := strings.TrimPrefix(goType, "*")
	switch t {
	case "string":
		return "string"
	case "bool":
		return "boolean"
	case "int", "int8", "int16", "int32", "int64",
		"uint", "uint8", "uint16", "uint32", "uint64":
		return "integer"
	case "float32", "float64":
		return "number"
	case "time.Time":
		return "string"
	}
	if strings.HasPrefix(t, "[]") {
		return "array"
	}
	if strings.HasPrefix(t, "map[") {
		return "object"
	}
	return ""
}

func collectStructs(fset *token.FileSet, files map[string]*ast.File, root string) map[string]StructDef {
	structs := map[string]StructDef{}

	// Sorted, not map order. Go randomises map iteration, so iterating `files`
	// directly made the output differ run to run wherever a name is declared in
	// more than one package - which is fatal for a report meant to be evidence.
	for _, path := range sortedPaths(files) {
		file := files[path]
		for _, decl := range file.Decls {
			gen, ok := decl.(*ast.GenDecl)
			if !ok || gen.Tok != token.TYPE {
				continue
			}
			for _, spec := range gen.Specs {
				ts, ok := spec.(*ast.TypeSpec)
				if !ok {
					continue
				}
				st, ok := ts.Type.(*ast.StructType)
				if !ok {
					continue
				}
				def := StructDef{
					Name: ts.Name.Name,
					File: rel(root, path),
					Line: fset.Position(ts.Pos()).Line,
				}
				for _, field := range st.Fields.List {
					goType := types.ExprString(field.Type)
					jsonName, omitempty, skipped := jsonTag(field)

					// An embedded field has no name of its own; it flattens into
					// the parent, which the AST alone cannot resolve. Recorded
					// by type name so a rule can decline to judge that struct
					// rather than compare an incomplete field set.
					names := field.Names
					if len(names) == 0 {
						def.Fields = append(def.Fields, Field{
							Name: goType, JSONName: "", GoType: goType,
							JSONType: goToJSONType(goType), Skipped: true,
							Line: fset.Position(field.Pos()).Line,
						})
						continue
					}
					for _, name := range names {
						resolved := jsonName
						if resolved == "" && !skipped {
							resolved = name.Name // Go's default: the field name verbatim
						}
						def.Fields = append(def.Fields, Field{
							Name: name.Name, JSONName: resolved, GoType: goType,
							JSONType: goToJSONType(goType), Omitempty: omitempty,
							Skipped: skipped || !name.IsExported(),
							Line:    fset.Position(name.Pos()).Line,
						})
					}
				}
				if existing, clash := structs[def.Name]; clash {
					if existing.File != def.File {
						existing.Ambiguous = true
						structs[def.Name] = existing // first in sorted order wins
					}
					continue
				}
				structs[def.Name] = def
			}
		}
	}
	return structs
}

// jsonTag reads the encoding/json struct tag. A tag of "-" means the field
// never reaches the wire.
func jsonTag(field *ast.Field) (name string, omitempty, skipped bool) {
	if field.Tag == nil {
		return "", false, false
	}
	raw, err := strconv.Unquote(field.Tag.Value)
	if err != nil {
		return "", false, false
	}
	tag := reflect.StructTag(raw).Get("json")
	if tag == "-" {
		return "", false, true
	}
	parts := strings.Split(tag, ",")
	name = parts[0]
	for _, opt := range parts[1:] {
		if opt == "omitempty" {
			omitempty = true
		}
	}
	return name, omitempty, false
}

func collectHandlerFacts(fset *token.FileSet, files map[string]*ast.File, root string) map[string]HandlerFacts {
	facts := map[string]HandlerFacts{}

	for _, path := range sortedPaths(files) {
		file := files[path]
		for _, decl := range file.Decls {
			fn, ok := decl.(*ast.FuncDecl)
			if !ok || fn.Body == nil {
				continue
			}
			f := HandlerFacts{
				Name:    fn.Name.Name,
				File:    rel(root, path),
				Line:    fset.Position(fn.Pos()).Line,
				EndLine: fset.Position(fn.End()).Line,
			}
			statuses := map[int]bool{}
			queries := map[string]bool{}
			headersRead := map[string]bool{}
			headersSet := map[string]bool{}

			ast.Inspect(fn.Body, func(n ast.Node) bool {
				switch node := n.(type) {
				case *ast.SelectorExpr:
					// http.StatusCreated
					if pkg, ok := node.X.(*ast.Ident); ok && pkg.Name == "http" {
						if code, known := statusNames[node.Sel.Name]; known {
							statuses[code] = true
						}
					}
				case *ast.CallExpr:
					classifyCall(node, queries, headersRead, headersSet)
				}
				return true
			})

			f.Statuses = sortedInts(statuses)
			f.SuccessCode = lowestSuccess(f.Statuses)
			f.QueryParams = sortedStrings(queries)
			f.HeadersRead = sortedStrings(headersRead)
			f.HeadersSet = sortedStrings(headersSet)
			if existing, clash := facts[fn.Name.Name]; clash {
				if existing.File != f.File {
					existing.Ambiguous = true
					facts[fn.Name.Name] = existing
				}
				continue
			}
			facts[fn.Name.Name] = f
		}
	}
	return facts
}

// classifyCall recognises the three accessor shapes that carry contract
// meaning. The receiver disambiguates them: Get on a Query() result reads a
// query parameter, Get on a .Header field reads a request header, and Set on a
// Header() result writes a response header.
func classifyCall(call *ast.CallExpr, queries, headersRead, headersSet map[string]bool) {
	sel, ok := call.Fun.(*ast.SelectorExpr)
	if !ok || len(call.Args) == 0 {
		return
	}
	lit, ok := stringArg(call.Args[0])
	if !ok || lit == "" {
		return
	}

	switch sel.Sel.Name {
	case "Get":
		switch recv := sel.X.(type) {
		case *ast.CallExpr:
			// r.URL.Query().Get("page")
			if inner, ok := recv.Fun.(*ast.SelectorExpr); ok {
				switch inner.Sel.Name {
				case "Query":
					queries[lit] = true
				case "Header":
					headersRead[lit] = true
				}
			}
		case *ast.SelectorExpr:
			// r.Header.Get("X-Api-Key")
			if recv.Sel.Name == "Header" {
				headersRead[lit] = true
			}
		}
	case "Set", "Add":
		// w.Header().Set("X-Signature", ...)
		if recv, ok := sel.X.(*ast.CallExpr); ok {
			if inner, ok := recv.Fun.(*ast.SelectorExpr); ok && inner.Sel.Name == "Header" {
				headersSet[lit] = true
			}
		}
	}
}

// lowestSuccess reports the handler's principal success code. Where a handler
// can emit more than one 2xx the lowest is taken, which is the convention swag
// itself follows.
func lowestSuccess(statuses []int) int {
	for _, code := range statuses {
		if code >= 200 && code < 300 {
			return code
		}
	}
	return 0
}

func sortedInts(set map[int]bool) []int {
	out := make([]int, 0, len(set))
	for k := range set {
		out = append(out, k)
	}
	sort.Ints(out)
	return out
}

func sortedStrings(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for k := range set {
		out = append(out, k)
	}
	sort.Strings(out)
	return out
}

// sortedPaths returns file paths in a stable order. Every collector uses it, so
// two runs over unchanged source produce byte-identical output.
func sortedPaths(files map[string]*ast.File) []string {
	paths := make([]string, 0, len(files))
	for p := range files {
		paths = append(paths, p)
	}
	sort.Strings(paths)
	return paths
}
