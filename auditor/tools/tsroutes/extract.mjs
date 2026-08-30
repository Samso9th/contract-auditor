#!/usr/bin/env node
// Extracts an HTTP route table from a TypeScript/JavaScript Express project,
// using the TypeScript compiler's own parser rather than regular expressions.
//
// It emits the same JSON shape as the Go extractor, so every layer above it -
// the drift rules, the agent, the scorer, the reporters - is unchanged.
//
// Recognises the Express idiom:
//
//   router.get('/:id', handler)                 // route, params normalised
//   router.use('/customers', customersRouter)   // prefix resolved across files
//   app.post('/pay', mw, handler)               // middleware skipped
//
// Usage: node extract.mjs --dir <src> [--strip-prefix /api/v1]

import fs from "node:fs";
import path from "node:path";
import { createRequire } from "node:module";

const args = process.argv.slice(2);
const argOf = (flag, fallback = "") => {
  const i = args.indexOf(flag);
  return i >= 0 && args[i + 1] ? args[i + 1] : fallback;
};
const ROOT = path.resolve(argOf("--dir", "."));
const STRIP = argOf("--strip-prefix", "");

// The TypeScript package is resolved from the target project when it has one,
// then from this tool's own install. Parsing TypeScript with anything less than
// a real parser is how a route table quietly goes wrong.
function loadTypeScript() {
  const here = path.dirname(new URL(import.meta.url).pathname);
  const candidates = [ROOT, path.dirname(ROOT), path.dirname(path.dirname(ROOT)), process.cwd(), here];
  for (const base of candidates) {
    try {
      return createRequire(path.join(base, "noop.js"))("typescript");
    } catch { /* try the next one */ }
  }
  try { return createRequire(import.meta.url)("typescript"); } catch { /* fall through */ }

  // Global install, which is how the container ships it. NODE_PATH is honoured
  // by CommonJS resolution but not reliably across every Node version and
  // install layout, so the global root is tried explicitly rather than assumed -
  // a container that cannot parse TypeScript would otherwise fail at audit time
  // with a message that points nowhere useful.
  const globalRoots = [
    process.env.NODE_PATH,
    "/usr/local/lib/node_modules",
    "/usr/lib/node_modules",
    "/opt/homebrew/lib/node_modules",
  ].filter(Boolean).flatMap((entry) => entry.split(path.delimiter));

  for (const root of globalRoots) {
    for (const candidate of [path.join(root, "typescript"),
                             path.join(root, "typescript", "lib", "typescript.js")]) {
      try {
        if (fs.existsSync(candidate)) return createRequire(path.join(root, "noop.js"))(candidate);
      } catch { /* try the next one */ }
    }
  }

  console.error(
    "typescript could not be resolved. Install it in the target project " +
    "(npm i -D typescript) or globally (npm i -g typescript). Searched: " +
    candidates.concat(globalRoots).join(", ")
  );
  process.exit(2);
}
const ts = loadTypeScript();

const HTTP_METHODS = new Set(["get", "post", "put", "patch", "delete", "head", "options", "all"]);

// Handler-body facts, the TypeScript counterpart of what facts.go reads from Go.
// Without these the rules can only settle route existence, and every response
// shape, status and header falls to a model reading source approximately - which
// measured 0.571 F1 against Go's 1.000 on the same injected drifts.

function jsonTypeOfNode(node) {
  if (!node) return "";
  if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node) ||
      ts.isTemplateExpression(node)) return "string";
  if (ts.isNumericLiteral(node)) return "number";
  if (node.kind === ts.SyntaxKind.TrueKeyword || node.kind === ts.SyntaxKind.FalseKeyword) return "boolean";
  if (ts.isArrayLiteralExpression(node)) return "array";
  if (ts.isObjectLiteralExpression(node)) return "object";
  if (ts.isPrefixUnaryExpression(node) && ts.isNumericLiteral(node.operand)) return "number";
  // `String(x)`, `Number(x)` and friends state the type at the call site.
  if (ts.isCallExpression(node) && ts.isIdentifier(node.expression)) {
    const fn = node.expression.text;
    if (fn === "String") return "string";
    if (fn === "Number" || fn === "parseInt" || fn === "parseFloat") return "number";
    if (fn === "Boolean") return "boolean";
  }
  return "";
}

// Fields of an object literal, with the JSON type of each value where the AST
// settles it. A spread or computed key makes the shape incomplete, and an
// incomplete shape must not be compared - a missing field would read as drift.
function fieldsOfObject(node, sf) {
  const fields = {};
  let complete = true;
  for (const prop of node.properties) {
    if (ts.isSpreadAssignment(prop)) { complete = false; continue; }
    const name = prop.name;
    let key = null;
    if (name && (ts.isIdentifier(name) || ts.isStringLiteral(name))) key = name.text;
    if (key === null) { complete = false; continue; }
    const value = ts.isPropertyAssignment(prop) ? prop.initializer
                : ts.isShorthandPropertyAssignment(prop) ? prop.name : null;
    fields[key] = {
      json_name: key,
      json_type: jsonTypeOfNode(value),
      line: sf.getLineAndCharacterOfPosition(prop.getStart(sf)).line + 1,
    };
  }
  return { fields, complete };
}

function sourceFiles(dir) {
  const out = [];
  const skip = new Set(["node_modules", "dist", "build", ".git", "coverage", "logs"]);
  (function walk(current) {
    let entries;
    try { entries = fs.readdirSync(current, { withFileTypes: true }); } catch { return; }
    for (const entry of entries.sort((a, b) => a.name.localeCompare(b.name))) {
      const full = path.join(current, entry.name);
      if (entry.isDirectory()) { if (!skip.has(entry.name)) walk(full); continue; }
      if (/\.(ts|tsx|js|mjs|cjs)$/.test(entry.name) &&
          !/\.(test|spec|d)\.(ts|tsx|js)$/.test(entry.name)) out.push(full);
    }
  })(dir);
  return out;
}

const literal = (node) =>
  node && (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) ? node.text : null;

// Express ':id' and '*' become OpenAPI '{id}', so paths compare to a spec.
function normalisePath(p) {
  let out = p.replace(/:([A-Za-z_][A-Za-z0-9_]*)\??/g, "{$1}").replace(/\/\*$/, "/{wildcard}");
  if (STRIP && out.startsWith(STRIP)) {
    const trimmed = out.slice(STRIP.length);
    if (trimmed.startsWith("/")) out = trimmed;
  }
  out = out.replace(/\/{2,}/g, "/");
  if (out.length > 1) out = out.replace(/\/$/, "");
  return out || "/";
}

// A middleware is named by an identifier (authenticate), a member access
// (auth.required) or a factory call (validate(schema)). The factory keeps its
// parentheses so a reader can tell the two apart in the route table.
function argName(a) {
  if (ts.isIdentifier(a)) return a.text;
  if (ts.isPropertyAccessExpression(a)) return a.name.text;
  if (ts.isCallExpression(a)) {
    const e = a.expression;
    if (ts.isIdentifier(e)) return `${e.text}()`;
    if (ts.isPropertyAccessExpression(e)) return `${e.name.text}()`;
  }
  return "";
}

function middlewareNames(args) {
  const out = [];
  // args[0] is the path and the last is the handler; everything between guards
  // the route. An inline arrow function is a handler, never a named guard.
  for (let i = 1; i < args.length - 1; i++) {
    const name = argName(args[i]);
    if (name) out.push(name);
  }
  return out;
}

function handlerName(args) {
  for (let i = args.length - 1; i >= 0; i--) {
    const a = args[i];
    if (ts.isIdentifier(a)) return a.text;
    if (ts.isPropertyAccessExpression(a)) return a.name.text;
    if (ts.isArrowFunction(a) || ts.isFunctionExpression(a)) return "<inline>";
    if (ts.isCallExpression(a)) {
      const e = a.expression;
      if (ts.isIdentifier(e)) return `${e.text}()`;
      if (ts.isPropertyAccessExpression(e)) return `${e.name.text}()`;
    }
  }
  return "";
}

// Pass 1: per file, record which local identifier each import binds to, the
// mount points declared with router.use(prefix, subRouter), and every route.
const files = sourceFiles(ROOT);
const perFile = new Map();
const perFileFacts = [];
// Every function declaration in the project, so a handler that builds its
// response in an imported module can still be followed. First declaration wins,
// in sorted file order, which keeps the result stable across runs.
const GLOBAL_FUNCTIONS = new Map();

for (const file of files) {
  let text;
  try { text = fs.readFileSync(file, "utf8"); } catch { continue; }
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
  const rel = path.relative(ROOT, file);
  const functions = new Map(); // name -> declaration node, for resolving helpers
  const facts = new Map();     // handler name -> body facts
  const imports = new Map();   // local name -> resolved file
  const mounts = [];           // { prefix, local }
  const routes = [];           // { method, path, handler, middleware, line }
  const routerMiddleware = [];  // router.use(mw): guards every route in the file
  // Barrel files forward a router without ever mounting it:
  //   export { default } from './customers.routes.js';
  // Following imports and router.use() alone stops dead at these, which left
  // most of a real project's routes looking unmounted and prefix-less.
  const reexports = [];

  const lineOf = (node) => sf.getLineAndCharacterOfPosition(node.getStart(sf)).line + 1;

  const resolveImport = (spec) => {
    if (!spec.startsWith(".")) return null;
    // Node ESM requires the .js suffix even in TypeScript sources, so the
    // import specifier rarely names the file that actually exists on disk.
    const base = path.resolve(path.dirname(file), spec.replace(/\.js$/, ""));
    for (const cand of [`${base}.ts`, `${base}.tsx`, `${base}.js`, `${base}.mjs`,
                        path.join(base, "index.ts"), path.join(base, "index.js")]) {
      if (fs.existsSync(cand)) return cand;
    }
    return null;
  };

  // Pass A: index every function declaration in the file, so a handler that
  // delegates its response shape to a helper can be followed one level.
  const indexFunctions = (node) => {
    if (ts.isFunctionDeclaration(node) && node.name) functions.set(node.name.text, node);
    if (ts.isVariableStatement(node)) {
      for (const decl of node.declarationList.declarations) {
        if (ts.isIdentifier(decl.name) && decl.initializer &&
            (ts.isArrowFunction(decl.initializer) || ts.isFunctionExpression(decl.initializer))) {
          functions.set(decl.name.text, decl.initializer);
        }
      }
    }
    ts.forEachChild(node, indexFunctions);
  };
  indexFunctions(sf);
  for (const [name, node] of functions) {
    if (!GLOBAL_FUNCTIONS.has(name)) GLOBAL_FUNCTIONS.set(name, { node, sf, rel });
  }

  // The object literal a function returns, following one level of local helper.
  const returnedShape = (fn, depth = 0, ownerSf = sf) => {
    if (!fn || depth > 2) return null;
    let found = null;
    const walk = (node) => {
      if (found) return;
      if (ts.isReturnStatement(node) && node.expression) {
        const expr = node.expression;
        if (ts.isObjectLiteralExpression(expr)) { found = fieldsOfObject(expr, ownerSf); return; }
        if (ts.isArrayLiteralExpression(expr) && expr.elements.length &&
            ts.isObjectLiteralExpression(expr.elements[0])) {
          found = fieldsOfObject(expr.elements[0], ownerSf); return;
        }
        if (ts.isCallExpression(expr) && ts.isIdentifier(expr.expression)) {
          const helper = functions.get(expr.expression.text)
                      ?? GLOBAL_FUNCTIONS.get(expr.expression.text)?.node;
          const helperSf = functions.has(expr.expression.text)
            ? sf : GLOBAL_FUNCTIONS.get(expr.expression.text)?.sf;
          if (helper) { found = returnedShape(helper, depth + 1, helperSf ?? sf); return; }
        }
      }
      ts.forEachChild(node, walk);
    };
    walk(fn);
    return found;
  };

  // The status a response call is chained off: `res.status(201).json(...)`.
  const chainedStatus = (expr) => {
    if (!expr || !ts.isCallExpression(expr)) return null;
    if (!ts.isPropertyAccessExpression(expr.expression)) return null;
    if (expr.expression.name.text !== "status") return null;
    const arg = expr.arguments[0];
    return arg && ts.isNumericLiteral(arg) ? Number(arg.text) : null;
  };

  // Pass B: read what each handler body demonstrably does.
  const readFacts = (fn, name) => {
    const statuses = new Set(), headersSet = new Set(), queryParams = new Set();
    let shape = null, shapeComplete = true;

    const walk = (node) => {
      if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)) {
        const method = node.expression.name.text;
        const arg = node.arguments[0];

        if (method === "status" && arg && ts.isNumericLiteral(arg)) {
          statuses.add(Number(arg.text));
        }
        if ((method === "set" || method === "header") && arg && ts.isStringLiteral(arg)) {
          headersSet.add(arg.text);
        }
        if ((method === "json" || method === "send") && arg) {
          let candidate = null;
          if (ts.isObjectLiteralExpression(arg)) candidate = fieldsOfObject(arg, sf);
          else if (ts.isArrayLiteralExpression(arg) && arg.elements.length &&
                   ts.isObjectLiteralExpression(arg.elements[0])) {
            candidate = fieldsOfObject(arg.elements[0], sf);
          } else if (ts.isCallExpression(arg) && ts.isIdentifier(arg.expression)) {
            const local = functions.get(arg.expression.text);
            const global = GLOBAL_FUNCTIONS.get(arg.expression.text);
            if (local) candidate = returnedShape(local);
            else if (global) candidate = returnedShape(global.node, 0, global.sf);
          }
          // Only the success response describes the contract; an error body is
          // a different shape and comparing it would manufacture drift.
          //
          // The status must come from this call's own receiver. Walking the tree
          // reaches `res.status(201).json(...)` at the `.json` node first and
          // descends into `.status(201)` afterwards, so a set accumulated during
          // the walk still holds only the earlier error statuses here - which
          // made every handler with a guard clause report its error shape.
          const chained = chainedStatus(node.expression.expression);
          const isSuccess = chained !== null && chained >= 200 && chained < 300;
          if (candidate && (isSuccess || (shape === null && chained === null))) {
            shape = candidate.fields;
            shapeComplete = candidate.complete;
          }
        }
      }
      // req.query.page and req.query["page"]
      if (ts.isPropertyAccessExpression(node) &&
          ts.isPropertyAccessExpression(node.expression) &&
          node.expression.name.text === "query") {
        queryParams.add(node.name.text);
      }
      if (ts.isElementAccessExpression(node) &&
          ts.isPropertyAccessExpression(node.expression) &&
          node.expression.name.text === "query" &&
          node.argumentExpression && ts.isStringLiteral(node.argumentExpression)) {
        queryParams.add(node.argumentExpression.text);
      }
      ts.forEachChild(node, walk);
    };
    walk(fn);

    const ordered = [...statuses].sort((a, b) => a - b);
    facts.set(name, {
      name,
      statuses: ordered,
      success_code: ordered.find((s) => s >= 200 && s < 300) || 0,
      query_params: [...queryParams].sort(),
      headers_read: [],
      headers_set: [...headersSet].sort(),
      response_fields: shape || {},
      response_complete: shape ? shapeComplete : false,
      file: rel,
      line: sf.getLineAndCharacterOfPosition(fn.getStart(sf)).line + 1,
      end_line: sf.getLineAndCharacterOfPosition(fn.getEnd()).line + 1,
      ambiguous: false,
    });
  };
  perFileFacts.push({ readFacts, functions, resolveHelperGlobally: null });

  const visit = (node) => {
    if (ts.isImportDeclaration(node) && node.importClause) {
      const target = resolveImport(literal(node.moduleSpecifier) ?? "");
      if (target) {
        if (node.importClause.name) imports.set(node.importClause.name.text, target);
        const named = node.importClause.namedBindings;
        if (named && ts.isNamedImports(named)) {
          for (const el of named.elements) imports.set(el.name.text, target);
        }
      }
    }

    if (ts.isExportDeclaration(node) && node.moduleSpecifier) {
      const target = resolveImport(literal(node.moduleSpecifier) ?? "");
      if (target) reexports.push(target);
    }

    if (ts.isCallExpression(node) && ts.isPropertyAccessExpression(node.expression)) {
      const method = node.expression.name.text;
      const first = node.arguments[0];

      if (method === "use" && node.arguments.length === 1 && first) {
        // router.use(authenticate): guards every route this router registers.
        // Express applies it only to routes declared after it; this treats it
        // as covering the file, which is how these files are actually written
        // and errs towards reporting a route as guarded rather than open.
        const name = argName(first);
        if (name) routerMiddleware.push(name);
      } else if (method === "use" && node.arguments.length >= 2) {
        const prefix = literal(first);
        if (prefix !== null && prefix.startsWith("/")) {
          for (let i = 1; i < node.arguments.length; i++) {
            const a = node.arguments[i];
            if (ts.isIdentifier(a)) mounts.push({ prefix, local: a.text });
          }
        }
      } else if (HTTP_METHODS.has(method) && node.arguments.length >= 2) {
        const routePath = literal(first);
        // A route registration always takes a path string plus a handler. The
        // guard keeps unrelated .get()/.delete() calls - a Map, a cache, an ORM
        // query builder - out of the route table.
        if (routePath !== null && (routePath === "" || routePath.startsWith("/"))) {
          routes.push({ method: method.toUpperCase(), path: routePath,
                        handler: handlerName(node.arguments),
                        // Everything between the path and the handler. Which of
                        // these is an auth guard is the caller's judgement, not
                        // this parser's: it records what is there.
                        middleware: middlewareNames(node.arguments),
                        line: lineOf(node) });
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  perFile.set(file, { rel, imports, mounts, routes, routerMiddleware, reexports,
                      facts, sf, functions });
}

// Pass 2: walk the mount graph from the entry router outward, so each route
// carries the full prefix its parents contributed.
const entry = files.find((f) => /routes[\\/]index\.(ts|js)$/.test(f))
           ?? files.find((f) => /(server|app|main|index)\.(ts|js)$/.test(f))
           ?? files[0];

const collected = [];
const seen = new Set();

function walk(file, prefix, depth) {
  if (!file || depth > 12) return;                 // cycle and runaway guard
  const key = `${file}::${prefix}`;
  if (seen.has(key)) return;
  seen.add(key);

  const entryData = perFile.get(file);
  if (!entryData) return;

  for (const route of entryData.routes) {
    collected.push({
      method: route.method,
      path: normalisePath(prefix + route.path),
      handler: route.handler,
      middleware: [...entryData.routerMiddleware, ...(route.middleware || [])],
      file: entryData.rel,
      line: route.line,
      style: "express",
      annotation: null,
    });
  }
  for (const mount of entryData.mounts) {
    const target = entryData.imports.get(mount.local);
    if (target) walk(target, prefix + mount.prefix, depth + 1);
  }
  // A re-export is transparent: the router it forwards belongs at this prefix,
  // not a nested one.
  for (const target of entryData.reexports) walk(target, prefix, depth + 1);
}

walk(entry, "", 0);

// A router reached by no mount still serves routes when it is mounted somewhere
// this parse could not follow. Reporting them without a prefix understates the
// path but is honest; dropping them would understate the API surface, which is
// the error this tool exists to prevent.
for (const [file, data] of perFile) {
  if (seen.has(`${file}::`) || !data.routes.length) continue;
  const reached = [...seen].some((k) => k.startsWith(`${file}::`));
  if (reached) continue;
  for (const route of data.routes) {
    collected.push({
      method: route.method, path: normalisePath(route.path), handler: route.handler,
      middleware: [...data.routerMiddleware, ...(route.middleware || [])],
      file: data.rel, line: route.line, style: "express-unmounted", annotation: null,
    });
  }
}

const deduped = [];
const key = new Set();
for (const r of collected.sort((a, b) => a.path.localeCompare(b.path) || a.method.localeCompare(b.method))) {
  const k = `${r.method} ${r.path}`;
  if (key.has(k)) continue;
  key.add(k);
  deduped.push(r);
}

// Facts are computed only now, once every file has contributed to the global
// function index.
for (const entry of perFileFacts) {
  for (const [name, fn] of entry.functions) entry.readFacts(fn, name);
}

// Merge handler facts across files. A name declared in more than one file
// cannot be attributed to one route, so it is marked ambiguous and the rules
// decline it rather than cite a possibly-wrong location.
const handlers = {};
for (const file of [...perFile.keys()].sort()) {
  for (const [name, fact] of perFile.get(file).facts) {
    if (handlers[name]) {
      if (handlers[name].file !== fact.file) handlers[name].ambiguous = true;
      continue;
    }
    handlers[name] = fact;
  }
}

process.stdout.write(JSON.stringify({
  dir: ROOT,
  strip_prefix: STRIP,
  language: "typescript",
  routes: deduped,
  annotations_unrouted: [],
  structs: {},
  handlers,
  route_count: deduped.length,
  routes_without_annotation: deduped.length,
}, null, 2));
