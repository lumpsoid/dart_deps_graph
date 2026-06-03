# dart_deps_graph

A Dart class-level dependency resolver that maps relationships between files based on **type usage** rather than raw imports.

This avoids the barrel-file explosion that import-only analysis suffers from: only the files that actually *declare* a referenced type appear in the dependency graph.

## How it works

1. Parses the entry file (and transitively referenced files) for:
   - **Declared types**: `class`, `enum`, `mixin`, `extension`, `typedef`
   - **Type usages**: `extends`/`implements`/`with`, constructor calls, field/parameter/variable type annotations
2. Follows the import/export chain to build a type index: `type name → declaring file`
   - Barrel files are transparent: re-exported types point to the file that *declares* them
3. For each declared type in the entry file, looks up every used type in the index — giving only the files that genuinely matter

## Requirements

- Python 3.10+
- No external dependencies

## Usage

```
python dart_deps_graph.py path/to/target.dart [options]
```

### Options

| Flag | Description |
|------|-------------|
| `--output` | Output format: `summary` (default), `flat`, `tree`, `class-tree` |
| `--level N` | Maximum dependency depth (omit or `0` for unlimited) |
| `--entry-types TYPES` | Comma-separated type names to trace from the entry file |
| `--pubspec PATH` | Explicit path to `pubspec.yaml` (auto-discovered by default) |
| `--verbose` | Print scanning progress to stderr |

### Output formats

**`summary`** — overview with per-file declared/used types and unresolved names

**`flat`** — newline-separated list of all resolved file paths (pipe-friendly)

**`tree`** — file-path tree rooted at the entry file, showing declared types per node

**`class-tree`** — same structure but with type names instead of file paths

### Examples

```sh
# Default summary output
python dart_deps_graph.py lib/main.dart

# Tree of files
python dart_deps_graph.py lib/main.dart --output tree

# Tree of class names
python dart_deps_graph.py lib/main.dart --output class-tree

# Only trace dependencies of specific types
python dart_deps_graph.py lib/main.dart --entry-types MyWidget,AppState

# Limit depth to direct dependencies only
python dart_deps_graph.py lib/main.dart --level 1

# Flat file list with verbose scanning log
python dart_deps_graph.py lib/main.dart --output flat --verbose
```

## Notes

- The tool auto-discovers `pubspec.yaml` by walking up from the target file
- Foreign packages (not in the same `package:` namespace) are excluded from the graph
- Common Dart/Flutter built-in types are filtered out to reduce noise
- String literals and comments are stripped before parsing to avoid false matches
