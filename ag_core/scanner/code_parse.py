"""Language-aware, fail-soft source parsing for the code graph.

Python is parsed with the stdlib ``ast``. Other languages go through
tree-sitter — the official core bindings plus the per-grammar packages
(``tree-sitter-javascript`` / ``-typescript`` / ``-go``), NOT the
third-party language-pack, whose 1.x line swapped in an undocumented PyO3
binding with a different node API. Dependencies are probed cheaply with
find_spec — same pattern as vector_store's chroma guard — and every parse
failure degrades to "no structural info" instead of an error, so callers
never need a guard. Without tree-sitter, non-Python files simply contribute
no defs/refs/imports (exactly the pre-R4 behavior of the code graph).
"""

import ast
import importlib.util
import os


def _module_available(name: str) -> bool:
    """Cheaply probe whether an optional dependency is installed (find_spec
    does not trigger the actual import)."""
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# grammar name -> (module, language-factory attr) — official grammar wheels
_GRAMMAR_MODULES = {
    "javascript": ("tree_sitter_javascript", "language"),
    "typescript": ("tree_sitter_typescript", "language_typescript"),
    "tsx": ("tree_sitter_typescript", "language_tsx"),
    "go": ("tree_sitter_go", "language"),
}

TREE_SITTER_AVAILABLE = _module_available("tree_sitter") and any(
    _module_available(mod) for mod, _ in set(_GRAMMAR_MODULES.values())
)

# extension -> tree-sitter grammar name
TS_LANGUAGES = {
    ".js": "javascript",
    ".jsx": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".go": "go",
}

# per-grammar: node type -> kind of the named definition it declares
_JS_DEFS = {
    "function_declaration": "function",
    "generator_function_declaration": "function",
    "class_declaration": "class",
    "method_definition": "method",
}
_TS_DEFS = {
    **_JS_DEFS,
    "interface_declaration": "interface",
    "enum_declaration": "enum",
    "type_alias_declaration": "type",
    "abstract_class_declaration": "class",
}
_DEF_TYPES = {
    "javascript": _JS_DEFS,
    "typescript": _TS_DEFS,
    "tsx": _TS_DEFS,
    "go": {
        "function_declaration": "function",
        "method_declaration": "method",
        "type_spec": "type",
    },
}

# field_identifier covers Go selector calls (pkg.Run -> "Run")
_REF_TYPES = {
    "identifier",
    "type_identifier",
    "property_identifier",
    "field_identifier",
}

# grammar name -> cached Parser (get_parser compiles nothing but does load
# the shared library; one per language is plenty)
_parsers = {}


def _get_parser(lang):
    if lang not in _parsers:
        import importlib

        import tree_sitter

        module_name, factory_attr = _GRAMMAR_MODULES[lang]
        grammar = importlib.import_module(module_name)
        language = tree_sitter.Language(getattr(grammar, factory_attr)())
        _parsers[lang] = tree_sitter.Parser(language)
    return _parsers[lang]


def _empty(lang=None):
    return {"lang": lang, "defs": [], "refs": set(), "imports": []}


def language_for(path: str):
    """Grammar name for a path ("python", a TS_LANGUAGES value, or None)."""
    p = (path or "").replace("\\", "/")
    if p.endswith(".py"):
        return "python"
    _, ext = os.path.splitext(p)
    return TS_LANGUAGES.get(ext.lower())


def parse_source(path: str, source):
    """Structural info for one file, never raises:

    ``{"lang", "defs": [(name, kind, line, signature)], "refs": set[str],
    "imports": [str]}`` — unsupported or unparsable input yields empty info.
    """
    lang = language_for(path)
    if source is None or not isinstance(source, str) or lang is None:
        return _empty(lang)
    try:
        if lang == "python":
            return _parse_python(source)
        if not TREE_SITTER_AVAILABLE:
            return _empty(lang)
        return _parse_treesitter(lang, source)
    except Exception:
        return _empty(lang)


def _sig_line(lines, lineno_1based):
    if 1 <= lineno_1based <= len(lines):
        return lines[lineno_1based - 1].strip()
    return ""


def _parse_python(source: str):
    tree = ast.parse(source)
    lines = source.splitlines()
    info = _empty("python")
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            info["defs"].append(
                (node.name, "function", node.lineno, _sig_line(lines, node.lineno))
            )
        elif isinstance(node, ast.ClassDef):
            info["defs"].append(
                (node.name, "class", node.lineno, _sig_line(lines, node.lineno))
            )
        elif isinstance(node, ast.Import):
            for alias in node.names:
                info["imports"].append(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                info["imports"].append(node.module)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            info["refs"].add(node.id)
    return info


def _node_text(node):
    try:
        return (node.text or b"").decode("utf-8", errors="ignore")
    except Exception:
        return ""


def _strip_quotes(s: str) -> str:
    return s.strip().strip("'\"`")


def _parse_treesitter(lang: str, source: str):
    parser = _get_parser(lang)
    tree = parser.parse(source.encode("utf-8", errors="ignore"))
    lines = source.splitlines()
    def_types = _DEF_TYPES[lang]
    info = _empty(lang)

    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(reversed(node.children))
        t = node.type

        kind = def_types.get(t)
        if kind is not None:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                line = node.start_point[0] + 1
                info["defs"].append(
                    (_node_text(name_node), kind, line, _sig_line(lines, line))
                )
        elif t == "variable_declarator":
            # const f = () => ... / const f = function () {...}
            value = node.child_by_field_name("value")
            name_node = node.child_by_field_name("name")
            if (
                value is not None
                and name_node is not None
                and value.type in ("arrow_function", "function_expression", "function")
            ):
                line = node.start_point[0] + 1
                info["defs"].append(
                    (_node_text(name_node), "function", line, _sig_line(lines, line))
                )
        elif t in _REF_TYPES:
            ref = _node_text(node)
            if ref:
                info["refs"].add(ref)

        if lang == "go":
            if t == "import_spec":
                path_node = node.child_by_field_name("path")
                if path_node is None:
                    for child in node.children:
                        if "string" in child.type:
                            path_node = child
                            break
                if path_node is not None:
                    spec = _strip_quotes(_node_text(path_node))
                    if spec:
                        info["imports"].append(spec)
        else:
            if t == "import_statement":
                src = node.child_by_field_name("source")
                if src is not None:
                    spec = _strip_quotes(_node_text(src))
                    if spec:
                        info["imports"].append(spec)
            elif t == "call_expression":
                fn = node.child_by_field_name("function")
                if fn is not None and _node_text(fn) in ("require", "import"):
                    args = node.child_by_field_name("arguments")
                    if args is not None:
                        for child in args.children:
                            if "string" in child.type:
                                spec = _strip_quotes(_node_text(child))
                                if spec:
                                    info["imports"].append(spec)
                                break
    return info


_JS_RESOLVE_SUFFIXES = (
    "",
    ".ts",
    ".tsx",
    ".js",
    ".jsx",
    ".mjs",
    ".cjs",
    "/index.ts",
    "/index.tsx",
    "/index.js",
    "/index.jsx",
)


def resolve_import(importer: str, spec: str, known_paths) -> list:
    """Repo files an import string points at (sorted normalized paths).

    Relative JS/TS specifiers ("./util") resolve against the importer's
    directory with the usual extension/index candidates; bare specifiers
    ("myapp/pkg") match repo directories by longest path-suffix (the Go
    package case). External packages resolve to nothing.
    """
    known = set(known_paths)
    out = set()
    spec = (spec or "").strip()
    if not spec:
        return []
    if spec.startswith("."):
        base = os.path.dirname(importer.replace("\\", "/"))
        target = os.path.normpath(os.path.join(base, spec)).replace("\\", "/")
        # Strip a leading "./" PREFIX only; `str.lstrip("./")` strips every
        # leading '.'/'/' and mangles paths under a dotfile directory.
        if target.startswith("./"):
            target = target[2:]
        for suffix in _JS_RESOLVE_SUFFIXES:
            cand = target + suffix
            if cand in known:
                out.add(cand)
                break
    else:
        parts = spec.strip("/").split("/")
        for i in range(len(parts)):
            tail = "/".join(parts[i:])
            matches = {
                p
                for p in known
                if os.path.dirname(p) == tail or os.path.dirname(p).endswith("/" + tail)
            }
            if matches:
                out |= matches
                break
    return sorted(out)
