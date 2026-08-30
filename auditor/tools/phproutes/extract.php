<?php
/**
 * Extract an HTTP route table from a Laravel project using PHP's own tokenizer.
 *
 * Emits the same JSON shape as the Go, TypeScript and Python extractors, so
 * every layer above it is unchanged.
 *
 * Uses token_get_all() rather than regular expressions: a route table read
 * approximately is worse than none, because an understated API surface reads as
 * a clean bill of health. The project's own code is never included or executed,
 * so a Laravel app whose vendor/ is missing can still be audited.
 *
 * Recognised:
 *   Route::get('/users', [UserController::class, 'index']);
 *   Route::post('/users', 'UserController@store');
 *   Route::prefix('v1')->group(function () { ... });
 *   Route::middleware('auth')->prefix('admin')->group(...);
 *   Route::apiResource('posts', PostController::class);
 *
 * Usage: php extract.php --dir <project> [--strip-prefix /api]
 */

declare(strict_types=1);

// `strip-prefix:` rather than `::` - an optional-value long option only accepts
// the `--opt=value` form, and returns false for `--opt value`, which then fails
// a string type check further down.
$options = getopt('', ['dir:', 'strip-prefix:']);
$root = realpath($options['dir'] ?? '.');
$strip = $options['strip-prefix'] ?? '';
if (!is_string($strip)) {
    $strip = '';
}

if ($root === false || !is_dir($root)) {
    fwrite(STDERR, "not a directory\n");
    exit(1);
}

const HTTP_METHODS = ['get', 'post', 'put', 'patch', 'delete', 'options', 'any'];

/** Route files first, then anything else that mentions Route:: */
function sourceFiles(string $root): array
{
    $skip = ['vendor', 'node_modules', 'storage', 'bootstrap/cache', '.git', 'tests'];
    $files = [];
    $iter = new RecursiveIteratorIterator(
        new RecursiveDirectoryIterator($root, FilesystemIterator::SKIP_DOTS)
    );
    foreach ($iter as $file) {
        if (!$file->isFile() || $file->getExtension() !== 'php') {
            continue;
        }
        $path = $file->getPathname();
        $rel = substr($path, strlen($root) + 1);
        foreach ($skip as $part) {
            if (str_starts_with($rel, $part . DIRECTORY_SEPARATOR) || str_contains($rel, DIRECTORY_SEPARATOR . $part . DIRECTORY_SEPARATOR)) {
                continue 2;
            }
        }
        $files[] = $path;
    }
    sort($files);
    return $files;
}

/** Significant tokens only: whitespace and comments carry no routing meaning. */
function significantTokens(string $source): array
{
    $out = [];
    foreach (token_get_all($source) as $token) {
        if (is_array($token)) {
            if (in_array($token[0], [T_WHITESPACE, T_COMMENT, T_DOC_COMMENT], true)) {
                continue;
            }
            $out[] = ['type' => $token[0], 'text' => $token[1], 'line' => $token[2]];
        } else {
            $out[] = ['type' => -1, 'text' => $token, 'line' => 0];
        }
    }
    return $out;
}

function stringValue(array $token): ?string
{
    if ($token['type'] !== T_CONSTANT_ENCAPSED_STRING) {
        return null;
    }
    return substr($token['text'], 1, -1);
}

function normalisePath(string $path, string $strip): string
{
    // Laravel's {id} and {id?} are already the OpenAPI form bar the optional marker.
    $path = preg_replace('/\{([^}:]+)(:[^}]+)?\??\}/', '{$1}', $path);
    if ($path !== '' && $path[0] !== '/') {
        $path = '/' . $path;
    }
    if ($strip !== '' && str_starts_with($path, $strip)) {
        $rest = substr($path, strlen($strip));
        if ($rest === '' || $rest[0] === '/') {
            $path = $rest;
        }
    }
    $path = preg_replace('#/{2,}#', '/', $path);
    if (strlen($path) > 1) {
        $path = rtrim($path, '/');
    }
    return $path === '' ? '/' : $path;
}

/**
 * Walk the token stream tracking group prefixes.
 *
 * Laravel expresses nesting as `Route::prefix('v1')->group(function () { ... })`,
 * so the prefix in force depends on how many group closures are open. Brace
 * depth is tracked and the prefix stack popped as each closes.
 */
/**
 * The prefix Laravel itself puts in front of a route file's paths.
 *
 * Nothing in routes/api.php says /api. The framework adds it when it loads the
 * file, and where that is declared depends on the generation: Laravel 11 and
 * later in bootstrap/app.php via withRouting(api: ...), everything before it in
 * app/Providers/RouteServiceProvider.php via ->prefix('api'). Both were
 * measured on real projects; one of each is in the test set.
 *
 * The declaration is what proves it, not the file name. A routes/api.php with
 * no Laravel bootstrap above it is not a Laravel application and gets nothing,
 * which is also what keeps this from rewriting a fixture that only borrows the
 * layout. And because the prefix is configurable, it is read rather than
 * assumed wherever the declaration names one.
 */
function laravelApiPrefix(string $absolute): string
{
    static $cache = [];

    $dir = dirname($absolute);
    for ($up = 0; $up < 8; $up++) {
        if (is_file($dir . '/artisan') || is_file($dir . '/composer.json')) {
            break;
        }
        $parent = dirname($dir);
        if ($parent === $dir) {
            return '';
        }
        $dir = $parent;
    }
    if (!is_file($dir . '/artisan') && !is_file($dir . '/composer.json')) {
        return '';
    }
    if (array_key_exists($dir, $cache)) {
        return $cache[$dir];
    }

    $prefix = '';
    $bootstrap = @file_get_contents($dir . '/bootstrap/app.php');
    if ($bootstrap !== false) {
        if (preg_match('/apiPrefix\s*:\s*[\'"]([^\'"]+)/', $bootstrap, $m)) {
            $prefix = '/' . trim($m[1], '/');
        } elseif (preg_match('/\bapi\s*:\s*(__DIR__|base_path|\[)/', $bootstrap)) {
            $prefix = '/api';
        }
    }
    if ($prefix === '') {
        $provider = @file_get_contents($dir . '/app/Providers/RouteServiceProvider.php');
        if ($provider !== false && preg_match('/->prefix\(\s*[\'"]([^\'"]+)/', $provider, $m)) {
            $prefix = '/' . trim($m[1], '/');
        }
    }

    $cache[$dir] = $prefix;
    return $prefix;
}

/**
 * Whether this file is one Laravel loads as its API routes: routes/api.php, or
 * the files a large project splits it into under routes/api/ and require()s
 * from there.
 */
function implicitPrefix(string $absolute): string
{
    $normalised = str_replace('\\', '/', $absolute);
    if (!preg_match('#(^|/)routes/api(\.php$|/)#', $normalised)) {
        return '';
    }
    return laravelApiPrefix($absolute);
}

/**
 * The prefix inside `Route::group(['prefix' => 'v1', ...], ...)`.
 *
 * Reads only the array literal that opens the group's argument list, and stops
 * at its matching bracket, so a 'prefix' key belonging to something nested
 * cannot be mistaken for this group's own.
 */
/**
 * The guards named by a middleware(...) call starting at $i.
 *
 * Accepts both spellings Laravel takes: middleware('auth:sanctum') and
 * middleware(['auth:sanctum', 'throttle:api']). A guard written as
 * 'auth:sanctum' keeps only the part before the colon, because the argument
 * after it is a parameter to the guard rather than a different guard.
 */
/**
 * Laravel's alias map: the short names a route uses, and the classes behind them.
 *
 * A Laravel route never names its guard in code. It writes 'auth:sanctum' or
 * 'api.ability', and the class that actually reads the credential is bound to
 * that string somewhere else - $middlewareAliases in app/Http/Kernel.php before
 * Laravel 11, $middleware->alias([...]) in bootstrap/app.php since. Left
 * unresolved there is nothing for the auditor to look up, so a Laravel project
 * could never have its contract guard identified, however plainly the code
 * reads the header.
 */
function laravelAliases(string $absolute): array
{
    static $cache = [];

    $dir = dirname($absolute);
    for ($up = 0; $up < 8; $up++) {
        if (is_file($dir . '/artisan') || is_file($dir . '/composer.json')) {
            break;
        }
        $parent = dirname($dir);
        if ($parent === $dir) {
            return [];
        }
        $dir = $parent;
    }
    if (array_key_exists($dir, $cache)) {
        return $cache[$dir];
    }

    $aliases = [];
    foreach (['/app/Http/Kernel.php', '/bootstrap/app.php'] as $candidate) {
        $source = @file_get_contents($dir . $candidate);
        if ($source === false) {
            continue;
        }
        if (preg_match_all('/[\'"]([A-Za-z0-9_.\-]+)[\'"]\s*=>\s*([A-Za-z0-9_\\\\]+)::class/', $source, $matches, PREG_SET_ORDER)) {
            foreach ($matches as $match) {
                $class = $match[2];
                $short = substr($class, strrpos($class, '\\') === false ? 0 : strrpos($class, '\\') + 1);
                $aliases[$match[1]] = $short;
            }
        }
    }

    $cache[$dir] = $aliases;
    return $aliases;
}

function middlewareNamesAt(array $tokens, int $i): array
{
    if (($tokens[$i + 1]['text'] ?? '') !== '(') {
        return [];
    }
    $out = [];
    $depth = 0;
    for ($j = $i + 1, $count = count($tokens); $j < $count; $j++) {
        $text = $tokens[$j]['text'];
        if ($text === '(' || $text === '[') {
            $depth++;
            continue;
        }
        if ($text === ')' || $text === ']') {
            $depth--;
            if ($depth <= 0) {
                break;
            }
            continue;
        }
        $literal = stringValue($tokens[$j]);
        if ($literal !== null && $literal !== '') {
            $out[] = explode(':', $literal)[0];
        }
    }
    return $out;
}

/**
 * The guards inside `Route::group(['middleware' => [...], ...], ...)`.
 */
/**
 * The guards a route attaches after the fact:
 *
 *   Route::get('/x', [C::class, 'm'])->middleware('auth:sanctum');
 *
 * Laravel chains these behind the registration, so they are not visible when
 * the route is emitted and have to be read forward to the end of the statement.
 * Stopping at the semicolon matters: without it a guard belonging to the next
 * route, or to a group opened below, would be attributed to this one.
 */
/**
 * Alias names replaced by the class behind them, so the auditor has something
 * it can find in the source. An alias with no binding keeps its own name: it is
 * still what the route says, and saying nothing would be worse.
 */
function resolveAliases(array $names, string $absolute): array
{
    $aliases = laravelAliases($absolute);
    $out = [];
    foreach (array_unique($names) as $name) {
        $out[] = $aliases[$name] ?? $name;
    }
    return array_values(array_unique($out));
}

function trailingMiddleware(array $tokens, int $i): array
{
    $out = [];
    $depth = 0;
    for ($j = $i + 1, $count = count($tokens); $j < $count; $j++) {
        $text = $tokens[$j]['text'];
        if ($text === '(' || $text === '[') { $depth++; continue; }
        if ($text === ')' || $text === ']') { $depth--; continue; }
        if ($text === ';' && $depth <= 0) { break; }
        if ($depth === 0 && $tokens[$j]['type'] === T_STRING
            && strtolower($tokens[$j]['text']) === 'middleware') {
            $out = array_merge($out, middlewareNamesAt($tokens, $j));
        }
    }
    return $out;
}

function groupArrayMiddleware(array $tokens, int $i): array
{
    if (($tokens[$i + 1]['text'] ?? '') !== '(' || ($tokens[$i + 2]['text'] ?? '') !== '[') {
        return [];
    }
    $depth = 0;
    for ($j = $i + 2, $count = count($tokens); $j < $count; $j++) {
        $text = $tokens[$j]['text'];
        if ($text === '[') { $depth++; continue; }
        if ($text === ']') { $depth--; if ($depth === 0) { return []; } continue; }
        if ($depth !== 1) { continue; }
        if (stringValue($tokens[$j]) === 'middleware' && ($tokens[$j + 1]['text'] ?? '') === '=>') {
            return middlewareNamesAt($tokens, $j + 1);
        }
    }
    return [];
}

function groupArrayPrefix(array $tokens, int $i): ?string
{
    if (($tokens[$i + 1]['text'] ?? '') !== '(' || ($tokens[$i + 2]['text'] ?? '') !== '[') {
        return null;
    }
    $depth = 0;
    for ($j = $i + 2, $count = count($tokens); $j < $count; $j++) {
        $text = $tokens[$j]['text'];
        if ($text === '[') {
            $depth++;
            continue;
        }
        if ($text === ']') {
            $depth--;
            if ($depth === 0) {
                return null;
            }
            continue;
        }
        if ($depth !== 1) {
            continue;
        }
        if (stringValue($tokens[$j]) === 'prefix' && ($tokens[$j + 1]['text'] ?? '') === '=>') {
            $literal = stringValue($tokens[$j + 2] ?? ['type' => 0, 'text' => '']);
            if ($literal !== null) {
                return '/' . trim($literal, '/');
            }
        }
    }
    return null;
}

function collectRoutes(array $tokens, string $rel, string $strip, string $absolute = ''): array
{
    $routes = [];
    // The absolute path, because the implicit prefix is decided by the file's
    // place in the project and --dir may already point inside routes/.
    $implicit = implicitPrefix($absolute !== '' ? $absolute : $rel);
    $prefixStack = [];        // ['prefix' => string, 'middleware' => string[], 'depth' => int]
    $depth = 0;
    $pendingPrefix = '';
    $pendingMiddleware = [];
    $count = count($tokens);

    for ($i = 0; $i < $count; $i++) {
        $token = $tokens[$i];

        if ($token['text'] === '{') {
            $depth++;
            continue;
        }
        if ($token['text'] === '}') {
            $depth--;
            // >= rather than >: a group is recorded at the depth of the
            // `group(` token, which is outside the closure brace it opens, so
            // its entry sits at the depth the closing brace returns to. With >
            // the entry survived its own closure and every later sibling group
            // inherited it, which compounded: two top-level groups both
            // prefixed v1 produced /v1/v1. Harmless while array-form prefixes
            // were being missed entirely, and wrong the moment they were read.
            while ($prefixStack && end($prefixStack)['depth'] >= $depth) {
                array_pop($prefixStack);
            }
            continue;
        }

        // T_MATCH as well as T_STRING: PHP 8 made `match` a reserved keyword,
        // so Route::match() does not tokenise as an ordinary method name and
        // was skipped before it could be recognised at all.
        if ($token['type'] !== T_STRING && $token['type'] !== T_MATCH) {
            continue;
        }

        $name = strtolower($token['text']);

        // Route::middleware('auth')->group(...) and ->middleware('auth') on a
        // single route. Which of the two it is depends on what follows, so both
        // are collected here and the group handler below claims them if a group
        // opens; otherwise the next route registration takes them.
        if ($name === 'middleware' && isset($tokens[$i + 1]) && $tokens[$i + 1]['text'] === '(') {
            foreach (middlewareNamesAt($tokens, $i) as $guard) {
                $pendingMiddleware[] = $guard;
            }
            continue;
        }

        // Route::prefix('v1') and ->prefix('v1'), whether chained or not.
        if ($name === 'prefix' && isset($tokens[$i + 1]) && $tokens[$i + 1]['text'] === '(') {
            $literal = stringValue($tokens[$i + 2] ?? ['type' => 0, 'text' => '']);
            if ($literal !== null) {
                $pendingPrefix = '/' . trim($literal, '/');
            }
            continue;
        }

        // group(...) opens a scope that owns whatever prefix was pending.
        //
        // Laravel spells a group's prefix two ways and both are current:
        // Route::prefix('v1')->group(...), handled above, and the array form
        // Route::group(['prefix' => 'v1'], ...). Reading only the chained one
        // dropped the version segment from every route in a project using the
        // other, which is most large Laravel codebases.
        if ($name === 'group') {
            if ($pendingPrefix === '') {
                $fromArray = groupArrayPrefix($tokens, $i);
                if ($fromArray !== null) {
                    $pendingPrefix = $fromArray;
                }
            }
            foreach (groupArrayMiddleware($tokens, $i) as $guard) {
                $pendingMiddleware[] = $guard;
            }
            $inherited = $prefixStack ? end($prefixStack)['prefix'] : '';
            $inheritedMw = $prefixStack ? end($prefixStack)['middleware'] : [];
            $prefixStack[] = [
                'prefix' => $inherited . $pendingPrefix,
                'middleware' => array_values(array_unique(array_merge($inheritedMw, $pendingMiddleware))),
                'depth' => $depth,
            ];
            $pendingPrefix = '';
            $pendingMiddleware = [];
            continue;
        }

        // Route::match(['get', 'post'], '/path', $action) puts the verbs in an
        // array and the path second, so neither is where the single-verb form
        // looks for them. The route then reads as never registered, and the
        // operations documenting it as never implemented - two findings on one
        // real project, both false.
        $matchVerbs = null;
        if ($name === 'match' && ($tokens[$i + 1]['text'] ?? '') === '('
            && ($tokens[$i + 2]['text'] ?? '') === '[') {
            $matchVerbs = [];
            $j = $i + 3;
            for ($count2 = count($tokens); $j < $count2; $j++) {
                if ($tokens[$j]['text'] === ']') {
                    break;
                }
                $verb = stringValue($tokens[$j]);
                if ($verb !== null && in_array(strtolower($verb), HTTP_METHODS, true)) {
                    $matchVerbs[] = strtolower($verb);
                }
            }
            // The path follows the closing bracket and the comma after it.
            $pathAt = $j + 2;
        }

        if (($matchVerbs !== null || in_array($name, HTTP_METHODS, true))
            && isset($tokens[$i + 1]) && $tokens[$i + 1]['text'] === '(') {
            $pathAt = $matchVerbs !== null ? $pathAt : $i + 2;
            $literal = stringValue($tokens[$pathAt] ?? ['type' => 0, 'text' => '']);
            if ($literal === null) {
                continue;
            }
            $prefix = $prefixStack ? end($prefixStack)['prefix'] : '';
            $groupMiddleware = $prefixStack ? end($prefixStack)['middleware'] : [];
            $routeMiddleware = trailingMiddleware($tokens, $pathAt);
            // A route's own guards belong to it and to nothing after it. Left
            // pending, they would be inherited by the next group that opened.
            $pendingMiddleware = [];
            $handler = '';
            // [UserController::class, 'index'] or 'UserController@index'
            for ($j = $pathAt + 1; $j < min($pathAt + 12, $count); $j++) {
                $candidate = stringValue($tokens[$j]);
                if ($candidate !== null && $candidate !== $literal) {
                    $handler = str_contains($candidate, '@')
                        ? explode('@', $candidate)[1]
                        : $candidate;
                    break;
                }
            }
            foreach ($matchVerbs ?? [$name] as $verb) {
            $routes[] = [
                'method' => strtoupper($verb === 'any' ? 'GET' : $verb),
                'path' => normalisePath($implicit . $prefix . '/' . ltrim($literal, '/'), $strip),
                'middleware' => resolveAliases(
                    array_merge($groupMiddleware, $routeMiddleware),
                    $absolute !== '' ? $absolute : $rel),
                'handler' => $handler,
                'file' => $rel,
                'line' => $token['line'],
                'style' => 'laravel',
                'annotation' => null,
            ];
            }
        }
    }
    return $routes;
}

/**
 * Handler-body facts, the PHP counterpart of what the other extractors read.
 *
 * Without these the rules settle only route existence, and every response shape,
 * status and default falls to a model reading source approximately. PHP has no
 * parser in core, so this is a focused recursive read of the token stream: it
 * recognises the shapes a controller actually returns and declines the rest.
 */

/** Index of the closing brace matching the one at $start. */
function matchBrace(array $tokens, int $start): int
{
    $depth = 0;
    for ($i = $start; $i < count($tokens); $i++) {
        if ($tokens[$i]['text'] === '{') $depth++;
        if ($tokens[$i]['text'] === '}') { $depth--; if ($depth === 0) return $i; }
    }
    return count($tokens) - 1;
}

/** JSON type of a literal token, or "" where the tokens cannot say. */
function literalJsonType(array $token): string
{
    if ($token['type'] === T_CONSTANT_ENCAPSED_STRING) return 'string';
    if ($token['type'] === T_LNUMBER || $token['type'] === T_DNUMBER) return 'number';
    if ($token['type'] === T_STRING && in_array(strtolower($token['text']), ['true','false'], true)) return 'boolean';
    if ($token['type'] === T_STRING && strtolower($token['text']) === 'null') return 'null';
    if ($token['text'] === '[') return 'array';
    return '';
}

/** Read the array literal whose '[' is at $start. */
function readArrayLiteral(array $tokens, int $start): array
{
    $fields = []; $complete = true; $depth = 0; $i = $start; $count = count($tokens);
    for (; $i < $count; $i++) {
        $text = $tokens[$i]['text'];
        if ($text === '[') { $depth++; continue; }
        if ($text === ']') { $depth--; if ($depth === 0) break; continue; }
        if ($depth !== 1) continue;
        if ($tokens[$i]['type'] === T_CONSTANT_ENCAPSED_STRING
            && isset($tokens[$i + 1]) && $tokens[$i + 1]['type'] === T_DOUBLE_ARROW) {
            $key = substr($tokens[$i]['text'], 1, -1);
            $value = $tokens[$i + 2] ?? null;
            $fields[$key] = ['json_name' => $key,
                             'json_type' => $value ? literalJsonType($value) : '',
                             'line' => $tokens[$i]['line']];
            continue;
        }
        // A spread makes the shape incomplete, and an incomplete shape must not
        // be compared: a field this could not see would read as a missing one.
        if ($tokens[$i]['type'] === T_ELLIPSIS) $complete = false;
    }
    return ['fields' => $fields, 'complete' => $complete];
}

/** Method bodies in a file, keyed by name. */
function methodBodies(array $tokens): array
{
    $out = [];
    for ($i = 0; $i < count($tokens); $i++) {
        if ($tokens[$i]['type'] !== T_FUNCTION) continue;
        $name = $tokens[$i + 1]['text'] ?? '';
        if ($name === '' || $name === '(') continue;
        for ($j = $i; $j < count($tokens); $j++) {
            if ($tokens[$j]['text'] === '{') {
                $out[$name] = ['start' => $j, 'end' => matchBrace($tokens, $j), 'line' => $tokens[$i]['line']];
                break;
            }
            if ($tokens[$j]['text'] === ';') break;
        }
    }
    return $out;
}

/** The shape a method returns, following one level of $this->helper(). */
function returnedShape(array $tokens, array $bodies, string $name, int $depth = 0): ?array
{
    if ($depth > 2 || !isset($bodies[$name])) return null;
    $start = $bodies[$name]['start']; $end = $bodies[$name]['end'];
    $fallback = null;

    for ($i = $start; $i < $end; $i++) {
        if ($tokens[$i]['type'] !== T_RETURN) continue;
        $j = $i + 1;
        if (($tokens[$j]['text'] ?? '') !== '[') continue;
        $inner = $j + 1;

        // `return [201, [...]]` and `return [200, $this->payout(...)]`
        if (($tokens[$inner]['type'] ?? 0) === T_LNUMBER && ($tokens[$inner + 1]['text'] ?? '') === ',') {
            $status = (int) $tokens[$inner]['text'];
            $bodyStart = $inner + 2;
            $shape = null;
            if (($tokens[$bodyStart]['text'] ?? '') === '[') {
                $shape = readArrayLiteral($tokens, $bodyStart);
            } elseif (($tokens[$bodyStart]['type'] ?? 0) === T_VARIABLE
                      && ($tokens[$bodyStart + 2]['type'] ?? 0) === T_STRING) {
                $shape = returnedShape($tokens, $bodies, $tokens[$bodyStart + 2]['text'], $depth + 1);
            }
            if ($shape === null) continue;

            // Only the success return describes the contract. Taking the first
            // return instead reports the guard clause's error shape, which is a
            // different shape entirely and would manufacture drift on every
            // handler that validates its input before doing any work.
            if ($status >= 200 && $status < 300) return $shape;
            if ($fallback === null) $fallback = $shape;
            continue;
        }
        return readArrayLiteral($tokens, $j);
    }
    return $fallback;
}

/** Statuses a method returns, from `return [<int>, ...]`. */
function returnedStatuses(array $tokens, array $bodies, string $name): array
{
    if (!isset($bodies[$name])) return [];
    $statuses = [];
    for ($i = $bodies[$name]['start']; $i < $bodies[$name]['end']; $i++) {
        if ($tokens[$i]['type'] !== T_RETURN) continue;
        if (($tokens[$i + 1]['text'] ?? '') !== '[') continue;
        $value = $tokens[$i + 2] ?? null;
        if ($value && $value['type'] === T_LNUMBER) {
            $code = (int) $value['text'];
            if ($code >= 100 && $code < 600) $statuses[] = $code;
        }
    }
    sort($statuses);
    return array_values(array_unique($statuses));
}

$routes = [];
$seen = [];
$handlers = [];
$handlersByLocation = [];
foreach (sourceFiles($root) as $path) {
    $source = @file_get_contents($path);
    if ($source === false) {
        continue;
    }
    $rel = substr($path, strlen($root) + 1);
    $tokens = significantTokens($source);

    foreach (methodBodies($tokens) as $name => $meta) {
        $bodies = methodBodies($tokens);
        $shape = returnedShape($tokens, $bodies, $name);
        $statuses = returnedStatuses($tokens, $bodies, $name);
        $success = 0;
        foreach ($statuses as $code) { if ($code >= 200 && $code < 300) { $success = $code; break; } }
        $fact = [
            'name' => $name, 'statuses' => $statuses, 'success_code' => $success,
            'query_params' => [], 'headers_read' => [], 'headers_set' => [],
            'response_fields' => (object) ($shape ? $shape['fields'] : []),
            'response_complete' => $shape ? $shape['complete'] : false,
            'file' => $rel, 'line' => $meta['line'],
            'end_line' => $tokens[$meta['end']]['line'] ?? $meta['line'],
            'ambiguous' => false,
        ];
        $handlersByLocation["{$rel}::{$name}"] = $fact;
        if (isset($handlers[$name])) {
            if ($handlers[$name]['file'] !== $rel) $handlers[$name]['ambiguous'] = true;
        } else {
            $handlers[$name] = $fact;
        }
    }

    if (!str_contains($source, 'Route::')) {
        continue;
    }
    foreach (collectRoutes($tokens, $rel, $strip, $path) as $route) {
        $key = $route['method'] . ' ' . $route['path'];
        if (isset($seen[$key])) {
            continue;
        }
        $seen[$key] = true;
        $routes[] = $route;
    }
}

usort($routes, fn($a, $b) => [$a['path'], $a['method']] <=> [$b['path'], $b['method']]);

echo json_encode([
    'dir' => $root,
    'strip_prefix' => $strip,
    'language' => 'php',
    'routes' => $routes,
    'annotations_unrouted' => [],
    'structs' => (object) [],
    'handlers' => (object) $handlers,
    'handlers_by_location' => (object) $handlersByLocation,
    'route_count' => count($routes),
    'routes_without_annotation' => count($routes),
], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), "\n";
