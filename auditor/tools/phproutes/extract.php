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

$routes = [];
$seen = [];
foreach (sourceFiles($root) as $path) {
    $source = @file_get_contents($path);
    if ($source === false || !str_contains($source, 'Route::')) {
        continue;
    }
    $rel = substr($path, strlen($root) + 1);
    foreach (collectRoutes(significantTokens($source), $rel, $strip) as $route) {
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
    'handlers' => (object) [],
    'route_count' => count($routes),
    'routes_without_annotation' => count($routes),
], JSON_PRETTY_PRINT | JSON_UNESCAPED_SLASHES), "\n";
