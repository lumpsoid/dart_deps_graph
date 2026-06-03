#!/usr/bin/env python3
"""
Dart Class-Level Dependency Resolver
-------------------------------------
Resolves dependencies between Dart files based on *type usage* rather than
raw imports.  This avoids the barrel-file explosion that import-only analysis
suffers from: only the files that actually *define* a referenced type end up
in the dependency graph.
How it works
------------
1.  Parse the entry file (and transitively referenced files) for:
      - Declared types  : class / enum / mixin / extension / typedef
      - Top-level names : functions, variables, constants
      - Type usages     : extends / implements / with / constructor calls /
                          field-type annotations / parameter types /
                          local-variable type annotations
2.  Follow the import/export chain to build a type-index:
      type name  →  the file that *declares* it
    Barrel files are handled transparently: when a file only re-exports
    symbols, the declaring file (not the barrel) gets the credit.
3.  For each declared type in the entry file, look up every *used* type in
    the index.  That gives you only the files that genuinely matter.
Usage
-----
    python dart_class_resolver.py path/to/target.dart
    python dart_class_resolver.py path/to/target.dart --output flat
    python dart_class_resolver.py path/to/target.dart --output tree
    python dart_class_resolver.py path/to/target.dart --output summary
    python dart_class_resolver.py path/to/target.dart --output class-tree
    python dart_class_resolver.py path/to/target.dart --verbose
    python dart_class_resolver.py path/to/target.dart --level 2
    python dart_class_resolver.py path/to/target.dart --entry-types MyWidget,AppState
"""
from __future__ import annotations
import argparse
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
# ---------------------------------------------------------------------------
# pubspec helpers (no external deps)
# ---------------------------------------------------------------------------
def read_pubspec_name(pubspec_path: Path) -> str:
    try:
        with open(pubspec_path, encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if not stripped or stripped.startswith("#"):
                    continue
                if not line[0].isspace() and ":" in line:
                    key, _, value = line.partition(":")
                    if key.strip() == "name":
                        name = value.strip().strip('"').strip("'")
                        if name:
                            return name
    except OSError as exc:
        raise FileNotFoundError(f"Cannot read pubspec.yaml: {exc}") from exc
    raise ValueError(f"Could not find 'name' field in {pubspec_path}")
def find_pubspec(start: Path) -> Path:
    current = start if start.is_dir() else start.parent
    while True:
        candidate = current / "pubspec.yaml"
        if candidate.exists():
            return candidate
        parent = current.parent
        if parent == current:
            raise FileNotFoundError(
                f"Could not find pubspec.yaml walking up from '{start}'."
            )
        current = parent
# ---------------------------------------------------------------------------
# Raw import / export extraction
# ---------------------------------------------------------------------------
_IMPORT_RE = re.compile(
    r"""^\s*(import|export)\s+['"]([^'"]+)['"]\s*"""
    r"""(?:(?:show|hide)\s+[\w\s,]+)?\s*;""",
    re.MULTILINE,
)
# Also capture 'show' / 'hide' combinators so we can honour them later
_IMPORT_FULL_RE = re.compile(
    r"""^\s*(import|export)\s+['"]([^'"]+)['"]\s*"""
    r"""(?:(show|hide)\s+([\w\s,]+))?\s*;""",
    re.MULTILINE,
)
@dataclass
class ImportDirective:
    kind: str          # 'import' or 'export'
    uri: str
    combinator: str | None   # 'show' | 'hide' | None
    names: list[str] = field(default_factory=list)
def extract_directives(dart_file: Path) -> list[ImportDirective]:
    try:
        text = dart_file.read_text(encoding="utf-8")
    except OSError as exc:
        print(f"  [warn] Cannot read {dart_file}: {exc}", file=sys.stderr)
        return []
    directives = []
    for m in _IMPORT_FULL_RE.finditer(text):
        kind, uri, combinator, names_raw = m.groups()
        names = [n.strip() for n in names_raw.split(",")] if names_raw else []
        directives.append(ImportDirective(kind, uri, combinator, names))
    return directives
# ---------------------------------------------------------------------------
# Dart source parser – declarations & usages
# ---------------------------------------------------------------------------
# ── Declarations ────────────────────────────────────────────────────────────
# Matches: class Foo, abstract class Foo, class Foo<T>, mixin Foo, enum Foo,
#          extension Foo on Bar, typedef Foo = ...
_DECL_RE = re.compile(
    r"""
    (?:^|\n)\s*
    (?:abstract\s+)?
    (?:base\s+|sealed\s+|final\s+|interface\s+)*
    (class|mixin|enum|extension|typedef)
    \s+
    (\w+)                   # type name
    """,
    re.VERBOSE,
)
# Top-level function:  ReturnType name( ...
_TOPLEVEL_FN_RE = re.compile(
    r"""
    (?:^|\n)
    (?![ \t])               # must start at column 0 (no indent)
    (?:[\w<>\[\]?,\s]+\s+)  # return type (greedy-ish)
    (\w+)\s*\(              # function name
    """,
    re.VERBOSE,
)
# Top-level variable / const:  final Foo bar = ...; / const bar = ...;
_TOPLEVEL_VAR_RE = re.compile(
    r"""
    (?:^|\n)
    (?![ \t])
    (?:const|final|var|late)\s+
    (?:[\w<>\[\]?,\s]+\s+)?   # optional type
    (\w+)\s*[=;]
    """,
    re.VERBOSE,
)
@dataclass
class TypeDeclaration:
    name: str
    kind: str            # class | mixin | enum | extension | typedef
    file: Path
def extract_declarations(dart_file: Path) -> list[TypeDeclaration]:
    """Return all type declarations found in *dart_file*."""
    try:
        raw = dart_file.read_text(encoding="utf-8")
    except OSError:
        return []
    text = _strip_noise(raw)
    decls = []
    for m in _DECL_RE.finditer(text):
        kind, name = m.group(1), m.group(2)
        decls.append(TypeDeclaration(name=name, kind=kind, file=dart_file))
    return decls
# ── Noise stripping ─────────────────────────────────────────────────────────
#
# Before we run ANY regex on Dart source we remove:
#   1. Block comments  /* … */  and doc comments  /** … */
#   2. Line comments   // …
#   3. String literals (single-quoted, double-quoted, raw, multi-line triple)
#
# String content and comments are replaced with whitespace so that line/column
# offsets are preserved (helps with debugging) but no identifier-like tokens
# inside them can accidentally match our patterns.
# Order matters: triple-quoted must come before single-quoted.
_NOISE_RE = re.compile(
    r"""
    /\*.*?\*/                   # block comment  /* … */
  | //[^\n]*                    # line comment   // …
  | r"""   + r'''"""[\s\S]*?"""'''   + r"""   # raw triple double-quoted string
  | r"""   + r"'''[\s\S]*?'''"       + r"""   # raw triple single-quoted string
  | """    + r'"""[\s\S]*?"""'       + r"""   # triple double-quoted string
  | '''[\s\S]*?'''              # triple single-quoted string
  | r"[^"]*"                    # raw double-quoted string
  | r'[^']*'                    # raw single-quoted string
  | "(?:[^"\\]|\\.)*"           # double-quoted string
  | '(?:[^'\\]|\\.)*'           # single-quoted string
    """,
    re.VERBOSE | re.DOTALL,
)
def _strip_noise(text: str) -> str:
    """
    Remove all string literals and comments from Dart source, replacing each
    match with the same number of spaces so line structure is preserved.
    """
    def _blank(m: re.Match) -> str:
        # Keep newlines intact so line-based patterns still work;
        # replace everything else with spaces.
        return re.sub(r"[^\n]", " ", m.group(0))
    return _NOISE_RE.sub(_blank, text)
# ── Usages ──────────────────────────────────────────────────────────────────
# extends / implements / with / on (mixin)
_INHERITANCE_RE = re.compile(
    r"""\b(?:extends|implements|with|on)\b\s+([\w<>,\s]+?)(?=\{|implements|with|extends|;)""",
    re.DOTALL,
)
# Constructor:  Foo(  /  Foo.named(  /  new Foo(  /  const Foo(
_CONSTRUCTOR_RE = re.compile(
    r"""\b(?:new\s+|const\s+)?([A-Z]\w*)(?:\.\w+)?\s*(?:<[^>]*>)?\s*\("""
)
# Type annotations in field/param/var declarations:   Foo bar  /  List<Foo>
# We look for CamelCase words used as types (Dart convention: types start uppercase)
_TYPE_ANNOTATION_RE = re.compile(
    r"""\b([A-Z]\w*)\b"""
)
# Exclude common Dart built-ins so they don't clutter the graph
_DART_BUILTINS: frozenset[str] = frozenset({
    "String", "int", "double", "bool", "num", "dynamic", "Object", "void",
    "Null", "Never", "List", "Map", "Set", "Iterable", "Future", "Stream",
    "Function", "Type", "Symbol", "DateTime", "Duration", "Uri",
    "Iterator", "Completer", "StreamController", "StreamSubscription", "RegExp",
    "BuildContext", "Widget", "StatelessWidget", "StatefulWidget", "State",
    "Key", "GlobalKey", "ValueKey", "UniqueKey",
    "Color", "Colors", "Icons", "Theme", "ThemeData",
    "Text", "Container", "Column", "Row", "Padding", "Center", "Scaffold",
    "AppBar", "MaterialApp", "Material", "Ink", "InkWell",
    "EdgeInsets", "EdgeInsetsGeometry", "BoxDecoration",
    "TextStyle", "TextAlign", "FontWeight",
    "SizedBox", "Expanded", "Flexible", "Stack", "Positioned",
    "Navigator", "Route", "PageRoute", "MaterialPageRoute",
    "ScaffoldMessenger", "SnackBar",
    "override", "required", "deprecated",
    # Common annotation classes
    "HiveType", "HiveField", "JsonKey", "JsonSerializable",
    "freezed", "immutable",
    "True", "False",
})
def extract_type_usages(dart_file: Path) -> set[str]:
    """
    Return the set of type names *used* (not declared) in *dart_file*.
    Filters out Dart builtins, private names, and anything inside string
    literals or comments.
    """
    try:
        raw = dart_file.read_text(encoding="utf-8")
    except OSError:
        return set()
    # !! Strip strings and comments FIRST so their content never matches !!
    text = _strip_noise(raw)
    usages: set[str] = set()
    # Inheritance / mixin / interface relationships
    for m in _INHERITANCE_RE.finditer(text):
        clause = m.group(1)
        for part in re.split(r"[,<>\s]+", clause):
            part = part.strip()
            if part and part[0].isupper():
                usages.add(part)
    # Constructor calls
    for m in _CONSTRUCTOR_RE.finditer(text):
        usages.add(m.group(1))
    # All CamelCase identifiers (broad net – filtered below)
    for m in _TYPE_ANNOTATION_RE.finditer(text):
        usages.add(m.group(1))
    # Remove builtins and single-letter type params (T, E, K, V, …)
    usages -= _DART_BUILTINS
    usages = {u for u in usages if len(u) > 1}
    return usages
# ---------------------------------------------------------------------------
# Import resolver (URI → absolute Path)
# ---------------------------------------------------------------------------
def resolve_uri(
    uri: str,
    current_file: Path,
    lib_dir: Path,
    package_name: str,
) -> Path | None:
    if uri.startswith("dart:") or uri.startswith("flutter:"):
        return None
    prefix = f"package:{package_name}/"
    if uri.startswith("package:"):
        if not uri.startswith(prefix):
            return None        # foreign package
        rel = uri[len(prefix):]
        return (lib_dir / rel).resolve()
    # Relative
    return (current_file.parent / uri).resolve()
# ---------------------------------------------------------------------------
# Type index builder
# ---------------------------------------------------------------------------
#
# Strategy
# --------
# BFS over the import/export graph starting from the entry file.
# For every reachable file, record which types it *declares*.
# Barrel files are transparent: even if Foo is re-exported through barrels,
# the index maps Foo → the file where it is actually declared.
#
# We store TWO indexes:
#   type_index  : type_name → Path of the declaring file
#   file_decls  : Path → list[TypeDeclaration]
@dataclass
class TypeIndex:
    type_to_file: dict[str, Path] = field(default_factory=dict)
    file_decls: dict[Path, list[TypeDeclaration]] = field(
        default_factory=lambda: defaultdict(list)
    )
    visited_for_index: set[Path] = field(default_factory=set)
def build_type_index(
    entry: Path,
    lib_dir: Path,
    package_name: str,
    verbose: bool = False,
) -> TypeIndex:
    """
    Scan all files reachable via imports/exports from *entry* and build a
    mapping of type-name → declaring-file.
    """
    index = TypeIndex()
    queue: list[Path] = [entry.resolve()]
    while queue:
        current = queue.pop(0)
        if current in index.visited_for_index:
            continue
        index.visited_for_index.add(current)
        if not current.exists():
            continue
        # Record declarations in this file
        decls = extract_declarations(current)
        index.file_decls[current] = decls
        for decl in decls:
            # First writer wins (prefers non-barrel definitions)
            if decl.name not in index.type_to_file:
                index.type_to_file[decl.name] = current
                if verbose:
                    print(
                        f"  [index] {decl.name:40s} ← {current}",
                        file=sys.stderr,
                    )
        # Enqueue imported/exported files
        for directive in extract_directives(current):
            resolved = resolve_uri(
                directive.uri, current, lib_dir, package_name
            )
            if resolved and resolved not in index.visited_for_index:
                queue.append(resolved)
    return index
# ---------------------------------------------------------------------------
# Class-level dependency graph
# ---------------------------------------------------------------------------
#
# For each file F in the entry's import closure:
#   declared_types(F) and used_types(F) are computed.
#   For each used type U:
#     look up defining_file(U) in the type index.
#     If defining_file != F → add edge F → defining_file.
#
# The result is:
#   graph : Path → set[Path]   (file → files it genuinely depends on)
@dataclass
class DependencyGraph:
    # file → set of files it depends on (direct edges)
    edges: dict[Path, set[Path]] = field(
        default_factory=lambda: defaultdict(set)
    )
    # file → declared type names (for display)
    file_types: dict[Path, list[str]] = field(
        default_factory=lambda: defaultdict(list)
    )
    # file → used type names that were resolved
    file_resolved_usages: dict[Path, dict[str, Path]] = field(
        default_factory=lambda: defaultdict(dict)
    )
    # type names that could not be resolved (not in index)
    unresolved: dict[Path, set[str]] = field(
        default_factory=lambda: defaultdict(set)
    )
def build_dependency_graph(
    entry: Path,
    lib_dir: Path,
    package_name: str,
    verbose: bool = False,
    max_depth: int | None = None,
    entry_types: list[str] | None = None,
) -> DependencyGraph:
    """
    Build a class-level dependency graph starting from *entry*.
    Parameters
    ----------
    entry_types : if given, only trace dependencies of these type names
                  (must be declared in the entry file).
    max_depth   : maximum edge depth (None = unlimited).
    """
    # Step 1 – build the global type index (scan all reachable files once)
    if verbose:
        print("[phase 1] Building type index …", file=sys.stderr)
    type_index = build_type_index(entry, lib_dir, package_name, verbose=verbose)
    if verbose:
        print(
            f"\n[phase 1] done – {len(type_index.type_to_file)} types indexed "
            f"across {len(type_index.visited_for_index)} files\n",
            file=sys.stderr,
        )
        print("[phase 2] Resolving class-level dependencies …", file=sys.stderr)
    graph = DependencyGraph()
    # BFS over the file graph, but only follow edges we discover
    # queue items: (file, depth)
    queue: list[tuple[Path, int]] = [(entry.resolve(), 0)]
    visited: set[Path] = set()
    while queue:
        current, depth = queue.pop(0)
        if current in visited:
            continue
        visited.add(current)
        # Record declared types
        decls = type_index.file_decls.get(current, [])
        graph.file_types[current] = [d.name for d in decls]
        # Which types should we trace?
        if current == entry.resolve() and entry_types:
            focus_types = set(entry_types)
        else:
            focus_types = None  # all types
        # Collect type usages from this file
        usages = extract_type_usages(current)
        # Exclude types declared in the same file (no self-edges)
        local_names = {d.name for d in decls}
        usages -= local_names
        # If we're focusing on specific types, filter usages to those
        # that are actually referenced in the class bodies of focus types
        # (best-effort: we don't parse individual class bodies, so we apply
        # the filter at the file level only for the entry file)
        if focus_types is not None:
            # Narrow: only keep usages from classes we care about
            # We re-parse to get per-class usages for the entry file
            usages = _usages_for_types(current, focus_types)
            usages -= local_names
        if verbose:
            depth_tag = f"depth={depth}"
            print(f"  [{depth_tag}] {current}", file=sys.stderr)
            print(f"    declared : {sorted(local_names)}", file=sys.stderr)
            print(f"    usages   : {sorted(usages)}", file=sys.stderr)
        # Resolve each used type to its defining file
        for type_name in usages:
            defining_file = type_index.type_to_file.get(type_name)
            if defining_file is None:
                graph.unresolved[current].add(type_name)
                continue
            if defining_file == current:
                continue   # same file
            graph.file_resolved_usages[current][type_name] = defining_file
            graph.edges[current].add(defining_file)
            # Enqueue the defining file for further traversal
            if (
                defining_file not in visited
                and (max_depth is None or depth < max_depth)
            ):
                queue.append((defining_file, depth + 1))
    if verbose:
        print(file=sys.stderr)
    return graph
def _usages_for_types(dart_file: Path, type_names: set[str]) -> set[str]:
    """
    Extract type usages from the class bodies of *type_names* only.
    Falls back to whole-file scan if we cannot isolate the bodies.
    """
    try:
        raw = dart_file.read_text(encoding="utf-8")
    except OSError:
        return set()
    # Strip strings/comments before any matching
    text = _strip_noise(raw)
    usages: set[str] = set()
    # Find each class body between its opening { and its matching }
    for tname in type_names:
        pattern = re.compile(
            rf"""\b(?:class|mixin|enum|extension)\s+{re.escape(tname)}\b[^{{]*\{{""",
        )
        m = pattern.search(text)
        if not m:
            # Couldn't locate the class – fall back to full-file usages
            usages |= extract_type_usages(dart_file)
            continue
        # Walk forward to find matching closing brace
        start = m.end() - 1  # position of '{'
        depth = 0
        end = start
        for i, ch in enumerate(text[start:], start):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        body = text[start:end + 1]
        for um in _TYPE_ANNOTATION_RE.finditer(body):
            usages.add(um.group(1))
    usages -= _DART_BUILTINS
    usages = {u for u in usages if len(u) > 1}
    return usages
# ---------------------------------------------------------------------------
# Output formatters
# ---------------------------------------------------------------------------
def all_files(graph: DependencyGraph) -> list[Path]:
    files: set[Path] = set(graph.edges.keys())
    for deps in graph.edges.values():
        files |= deps
    return sorted(files)
def format_flat(graph: DependencyGraph, entry: Path) -> str:
    return "\n".join(str(p) for p in all_files(graph))
def format_tree(
    graph: DependencyGraph,
    node: Path,
    prefix: str = "",
    visited: frozenset[Path] | None = None,
) -> str:
    if visited is None:
        visited = frozenset()
    deps = sorted(graph.edges.get(node, set()))
    type_tags = ", ".join(graph.file_types.get(node, [])) or "—"
    header = f"{node}  [{type_tags}]"
    if node in visited and deps:
        return header + "  ↩ (already expanded)"
    lines = [header]
    visited = visited | {node}
    for i, dep in enumerate(deps):
        connector = "└── " if i == len(deps) - 1 else "├── "
        extension = "    " if i == len(deps) - 1 else "│   "
        sub = format_tree(graph, dep, prefix + extension, visited)
        sub_lines = sub.splitlines()
        lines.append(prefix + connector + sub_lines[0])
        for sl in sub_lines[1:]:
            lines.append(prefix + extension + sl)
    return "\n".join(lines)
def format_summary(
    graph: DependencyGraph,
    entry: Path,
    max_depth: int | None,
    entry_types: list[str] | None,
) -> str:
    files = all_files(graph)
    depth_str = str(max_depth) if max_depth is not None else "unlimited"
    lines = [
        f"Entry point   : {entry.resolve()}",
        f"Entry types   : {', '.join(entry_types) if entry_types else 'all'}",
        f"Depth limit   : {depth_str}",
        f"Unique files  : {len(files)}",
        "",
    ]
    # Per-file detail
    lines.append("Dependency edges (file → depends on):")
    for src in sorted(graph.edges):
        declared = ", ".join(graph.file_types.get(src, [])) or "—"
        lines.append(f"\n  {src}")
        lines.append(f"    declares : {declared}")
        for type_name, def_file in sorted(
            graph.file_resolved_usages.get(src, {}).items()
        ):
            lines.append(f"    uses {type_name:35s} ← {def_file}")
        if graph.unresolved.get(src):
            unresolved_display = ", ".join(sorted(graph.unresolved[src]))
            lines.append(f"    unresolved types: {unresolved_display}")
    lines += [
        "",
        "All resolved files:",
    ]
    for p in files:
        declared = ", ".join(graph.file_types.get(p, [])) or "—"
        lines.append(f"  {p}  [{declared}]")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Class-tree formatter
# ---------------------------------------------------------------------------
def _build_type_dep_graph(
    graph: DependencyGraph,
) -> dict[str, set[str]]:
    """
    Invert graph.file_resolved_usages into a type-name → set[type-name] map.

    For every (src_file, type_name, def_file) triple in the resolved usages,
    we emit edges from each type declared in src_file to *type_name*, but only
    when def_file differs from src_file (cross-file dependency).
    """
    # Build a quick reverse map: file → declared type names
    file_to_types: dict[Path, list[str]] = graph.file_types

    type_deps: dict[str, set[str]] = defaultdict(set)

    for src_file, usages in graph.file_resolved_usages.items():
        src_types = file_to_types.get(src_file, [])
        if not src_types:
            # File has no named types – skip; nothing meaningful to anchor to
            continue
        for used_type, _def_file in usages.items():
            for src_type in src_types:
                if src_type != used_type:
                    type_deps[src_type].add(used_type)

    return type_deps


def _render_class_tree(
    type_name: str,
    type_deps: dict[str, set[str]],
    visited: frozenset[str] | None = None,
) -> list[str]:
    """
    Recursively render one node of the class-name tree.
    Returns a list of lines relative to this node (no leading prefix).
    The caller is responsible for prepending connector/extension strings.
    """
    if visited is None:
        visited = frozenset()

    children = sorted(type_deps.get(type_name, set()))

    if type_name in visited and children:
        return [f"{type_name}  ↩ (already expanded)"]

    lines = [type_name]
    visited = visited | {type_name}

    for i, child in enumerate(children):
        is_last = i == len(children) - 1
        connector = "└── " if is_last else "├── "
        extension = "    " if is_last else "│   "
        sub_lines = _render_class_tree(child, type_deps, visited)
        lines.append(connector + sub_lines[0])
        for sl in sub_lines[1:]:
            lines.append(extension + sl)

    return lines


def format_class_tree(
    graph: DependencyGraph,
    entry: Path,
) -> str:
    """
    Print a tree of class/type names rather than file paths.

    Root nodes are the types declared in the entry file.  Their children are
    the types they depend on (resolved cross-file), and so on transitively.
    Types that appear in multiple branches are expanded the first time and
    marked with ↩ on subsequent appearances.
    """
    type_deps = _build_type_dep_graph(graph)

    # Root: types declared in the entry file, in alphabetical order
    entry_resolved = entry.resolve()
    root_types = sorted(graph.file_types.get(entry_resolved, []))

    if not root_types:
        return "(no types declared in entry file)"

    sections: list[str] = []
    # Track globally visited types so siblings don't re-expand the same subtree
    globally_visited: frozenset[str] = frozenset()

    for root in root_types:
        sub_lines = _render_class_tree(root, type_deps, visited=globally_visited)
        sections.append("\n".join(sub_lines))
        # Collect every type name that appeared in this subtree so the next
        # root won't re-expand it
        globally_visited = globally_visited | _collect_names(
            root, type_deps, globally_visited
        )

    return "\n\n".join(sections)


def _collect_names(
    type_name: str,
    type_deps: dict[str, set[str]],
    already_visited: frozenset[str],
) -> frozenset[str]:
    """Return the set of all type names reachable from *type_name* (inclusive)."""
    seen: set[str] = set()
    stack = [type_name]
    while stack:
        current = stack.pop()
        if current in seen or current in already_visited:
            continue
        seen.add(current)
        stack.extend(type_deps.get(current, set()))
    return frozenset(seen)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="dart_class_resolver",
        description=(
            "Resolve Dart file dependencies at the *class / type* level, "
            "avoiding the barrel-file explosion of import-only analysis."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  python dart_class_resolver.py lib/main.dart
  python dart_class_resolver.py lib/main.dart --output tree
  python dart_class_resolver.py lib/main.dart --output class-tree
  python dart_class_resolver.py lib/main.dart --output flat --verbose
  python dart_class_resolver.py lib/main.dart --level 2
  python dart_class_resolver.py lib/main.dart --entry-types MyWidget,AppState
        """,
    )
    parser.add_argument("target", metavar="TARGET_FILE",
                        help="Path to the Dart file to analyse.")
    parser.add_argument("--output", choices=["flat", "tree", "summary", "class-tree"],
                        default="summary",
                        help="Output format (default: summary).")
    parser.add_argument("--pubspec", metavar="PUBSPEC_PATH", default=None,
                        help="Explicit path to pubspec.yaml.")
    parser.add_argument("--level", metavar="N", type=int, default=None,
                        help=(
                            "Maximum dependency depth. "
                            "1 = only types directly used by the entry file; "
                            "2 = + types used by those types; etc. "
                            "Omit / 0 for unlimited."
                        ))
    parser.add_argument("--entry-types", metavar="TYPES", default=None,
                        help=(
                            "Comma-separated list of type names to trace from "
                            "the entry file. If omitted, all declared types "
                            "are traced."
                        ))
    parser.add_argument("--verbose", action="store_true",
                        help="Print scanning progress to stderr.")
    return parser
def main() -> None:
    parser = build_parser()
    args = parser.parse_args()
    # ── --level ──────────────────────────────────────────────────────────────
    max_depth: int | None = None
    if args.level is not None:
        if args.level < 0:
            parser.error("--level must be a non-negative integer.")
        max_depth = args.level if args.level > 0 else None
    # ── target ───────────────────────────────────────────────────────────────
    target = Path(args.target).resolve()
    if not target.exists():
        parser.error(f"Target file not found: {target}")
    if not target.is_file():
        parser.error(f"Target is not a file: {target}")
    if target.suffix != ".dart":
        print(f"[warn] Target does not have a .dart extension: {target}",
              file=sys.stderr)
    # ── pubspec ───────────────────────────────────────────────────────────────
    if args.pubspec:
        pubspec_path = Path(args.pubspec).resolve()
        if not pubspec_path.exists():
            parser.error(f"Specified pubspec.yaml not found: {pubspec_path}")
    else:
        try:
            pubspec_path = find_pubspec(target)
        except FileNotFoundError as exc:
            parser.error(str(exc))
    project_root = pubspec_path.parent
    lib_dir = project_root / "lib"
    if not lib_dir.exists():
        print(f"[warn] No 'lib/' directory under {project_root}; "
              "using project root as lib dir.", file=sys.stderr)
        lib_dir = project_root
    try:
        package_name = read_pubspec_name(pubspec_path)
    except (FileNotFoundError, ValueError) as exc:
        parser.error(str(exc))
    # ── --entry-types ────────────────────────────────────────────────────────
    entry_types: list[str] | None = None
    if args.entry_types:
        entry_types = [t.strip() for t in args.entry_types.split(",") if t.strip()]
    if args.verbose:
        print(f"[info] pubspec     : {pubspec_path}", file=sys.stderr)
        print(f"[info] package     : {package_name}", file=sys.stderr)
        print(f"[info] lib dir     : {lib_dir}", file=sys.stderr)
        print(f"[info] depth limit : {max_depth or 'unlimited'}", file=sys.stderr)
        print(f"[info] entry types : {entry_types or 'all'}", file=sys.stderr)
        print(file=sys.stderr)
    # ── Run ──────────────────────────────────────────────────────────────────
    graph = build_dependency_graph(
        entry=target,
        lib_dir=lib_dir,
        package_name=package_name,
        verbose=args.verbose,
        max_depth=max_depth,
        entry_types=entry_types,
    )
    # ── Output ───────────────────────────────────────────────────────────────
    if args.output == "flat":
        print(format_flat(graph, target))
    elif args.output == "tree":
        print(format_tree(graph, target.resolve()))
    elif args.output == "class-tree":
        print(format_class_tree(graph, target))
    else:
        print(format_summary(graph, target, max_depth, entry_types))
if __name__ == "__main__":
    main()
