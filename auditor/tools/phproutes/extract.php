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
function collectRoutes(array $tokens, string $rel, string $strip): array
{
    $routes = [];
    $prefixStack = [];        // ['prefix' => string, 'depth' => int]
    $depth = 0;
    $pendingPrefix = '';
    $count = count($tokens);

    for ($i = 0; $i < $count; $i++) {
        $token = $tokens[$i];

        if ($token['text'] === '{') {
            $depth++;
            continue;
        }
        if ($token['text'] === '}') {
            $depth--;
            while ($prefixStack && end($prefixStack)['depth'] > $depth) {
                array_pop($prefixStack);
            }
            continue;
        }

        if ($token['type'] !== T_STRING) {
            continue;
        }

        $name = strtolower($token['text']);

        // Route::prefix('v1') and ->prefix('v1'), whether chained or not.
        if ($name === 'prefix' && isset($tokens[$i + 1]) && $tokens[$i + 1]['text'] === '(') {
            $literal = stringValue($tokens[$i + 2] ?? ['type' => 0, 'text' => '']);
            if ($literal !== null) {
                $pendingPrefix = '/' . trim($literal, '/');
            }
            continue;
        }

        // group(...) opens a scope that owns whatever prefix was pending.
        if ($name === 'group') {
            $inherited = $prefixStack ? end($prefixStack)['prefix'] : '';
            $prefixStack[] = ['prefix' => $inherited . $pendingPrefix, 'depth' => $depth];
            $pendingPrefix = '';
            continue;
        }

        if (in_array($name, HTTP_METHODS, true) && isset($tokens[$i + 1]) && $tokens[$i + 1]['text'] === '(') {
            $literal = stringValue($tokens[$i + 2] ?? ['type' => 0, 'text' => '']);
            if ($literal === null) {
                continue;
            }
            $prefix = $prefixStack ? end($prefixStack)['prefix'] : '';
            $handler = '';
            // [UserController::class, 'index'] or 'UserController@index'
            for ($j = $i + 3; $j < min($i + 14, $count); $j++) {
                $candidate = stringValue($tokens[$j]);
                if ($candidate !== null && $candidate !== $literal) {
                    $handler = str_contains($candidate, '@')
                        ? explode('@', $candidate)[1]
                        : $candidate;
                    break;
                }
            }
            $routes[] = [
                'method' => strtoupper($name === 'any' ? 'GET' : $name),
                'path' => normalisePath($prefix . '/' . ltrim($literal, '/'), $strip),
                'handler' => $handler,
                'file' => $rel,
                'line' => $token['line'],
                'style' => 'laravel',
                'annotation' => null,
            ];
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
    foreach (collectRoutes($tokens, $rel, $strip) as $route) {
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
