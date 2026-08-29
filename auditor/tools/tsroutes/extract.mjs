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
  const candidates = [ROOT, path.dirname(ROOT), process.cwd(), path.dirname(new URL(import.meta.url).pathname)];
  for (const base of candidates) {
    try {
      return createRequire(path.join(base, "noop.js"))("typescript");
    } catch { /* try the next one */ }
  }
  try { return createRequire(import.meta.url)("typescript"); } catch { /* fall through */ }
  console.error("typescript not resolvable. Install it in the target project or alongside this tool.");
  process.exit(2);
}
const ts = loadTypeScript();

const HTTP_METHODS = new Set(["get", "post", "put", "patch", "delete", "head", "options", "all"]);

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

for (const file of files) {
  let text;
  try { text = fs.readFileSync(file, "utf8"); } catch { continue; }
  const sf = ts.createSourceFile(file, text, ts.ScriptTarget.Latest, true);
  const rel = path.relative(ROOT, file);
  const imports = new Map();   // local name -> resolved file
  const mounts = [];           // { prefix, local }
  const routes = [];           // { method, path, handler, line }
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

      if (method === "use" && node.arguments.length >= 2) {
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
                        handler: handlerName(node.arguments), line: lineOf(node) });
        }
      }
    }
    ts.forEachChild(node, visit);
  };
  visit(sf);
  perFile.set(file, { rel, imports, mounts, routes, reexports });
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

process.stdout.write(JSON.stringify({
  dir: ROOT,
  strip_prefix: STRIP,
  language: "typescript",
  routes: deduped,
  annotations_unrouted: [],
  structs: {},
  handlers: {},
  route_count: deduped.length,
  routes_without_annotation: deduped.length,
}, null, 2));
