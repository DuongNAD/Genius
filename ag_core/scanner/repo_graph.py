"""Graph-aware, token-budgeted context selection.

The pipeline used to ship the ENTIRE scanned workspace to every agent call.
This module ranks files by structural relevance instead — a personalized
PageRank over the intra-repo import/symbol-reference graph, seeded by the
files the current task actually talks about — and spends a token budget in
rank order: seed files in full, then full text while it fits, then
class/def signature skeletons, then nothing.

The approach follows aider's repo map (symbol graph + personalized PageRank
+ budgeted signature-only rendering; ~50x boost for task files, ~10x for
mentioned identifiers) and the CodexGraph / LocAgent line of work on
graph-guided repository context. Implemented dependency-free with stdlib
``ast``: the projects this pipeline generates are Python.

Safety posture: when the whole workspace fits the budget the input dict is
returned UNCHANGED (identity passthrough — small workspaces, and therefore
the existing test suite, see byte-identical behavior), and any unexpected
internal failure also returns the input unchanged.
"""

import ast
import os
import re

from ag_core.scanner import code_parse
from ag_core.scanner.project_scanner import ProjectChunker

# Spent per agent call; only kicks in on workspaces bigger than this.
DEFAULT_TOKEN_BUDGET = 32000

_SEED_BOOST = 50.0  # files the task names explicitly (aider: files in chat)
_MENTION_BOOST = 10.0  # files defining an identifier the task text mentions
_DAMPING = 0.85
_ITERATIONS = 20

_PATH_MENTION_RE = re.compile(
    r"[A-Za-z0-9_\-./\\]+\.(?:py|md|toml|cfg|ini|yaml|yml|json|txt)"
)
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{2,}")

_chunker = None


def _count_tokens(text: str) -> int:
    global _chunker
    if _chunker is None:
        _chunker = ProjectChunker()
    return _chunker.count_tokens(text)


def _budget_from_env() -> int:
    raw = os.environ.get("GENIUS_CONTEXT_TOKEN_BUDGET")
    if raw is None or not raw.strip():
        return DEFAULT_TOKEN_BUDGET
    try:
        return int(raw)
    except ValueError:
        return DEFAULT_TOKEN_BUDGET


def _norm(path: str) -> str:
    # Strip a leading "./" PREFIX only. `str.lstrip("./")` strips every leading
    # '.'/'/' character, which mangles any path under a dotfile directory
    # (".github/x" -> "github/x") and silently drops that file from the graph.
    p = path.replace("\\", "/")
    if p.startswith("./"):
        p = p[2:]
    return p


def _module_names(path: str) -> set:
    """Dotted-module candidates a file can be imported as (suffix chain)."""
    p = _norm(path)
    if not p.endswith(".py"):
        return set()
    stem = p[: -len(".py")]
    if stem.endswith("/__init__"):
        stem = stem[: -len("/__init__")]
    dotted = stem.strip("/").replace("/", ".")
    if not dotted:
        return set()
    parts = dotted.split(".")
    return {".".join(parts[i:]) for i in range(len(parts))}


def _parse_py(source: str):
    """(defs, refs, imports) for one module; raises on unparsable source."""
    tree = ast.parse(source)
    defs, refs, imports = set(), set(), set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            defs.add(node.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                imports.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.module and node.level == 0:
                imports.add(node.module)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            refs.add(node.id)
    return defs, refs, imports


def _skeleton(source: str) -> str:
    """Signature-only rendering: module docstring line, top-level constants,
    class/def signatures. Falls back to a head-of-file slice on parse
    failure."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return "\n".join(source.splitlines()[:40])

    out = []
    mod_doc = ast.get_docstring(tree)
    if mod_doc:
        out.append('"""' + mod_doc.splitlines()[0] + '"""')

    # Skeletons must stay SMALL by construction (they are the fallback when
    # the budget is nearly spent): cap emitted top-level constants so a
    # constant-heavy module cannot balloon its own skeleton.
    assigns_emitted = 0
    max_assigns = 20

    def emit(container, indent):
        nonlocal assigns_emitted
        for child in container.body:
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                prefix = (
                    "async def" if isinstance(child, ast.AsyncFunctionDef) else "def"
                )
                args = ", ".join(a.arg for a in child.args.args)
                out.append(f"{indent}{prefix} {child.name}({args}): ...")
            elif isinstance(child, ast.ClassDef):
                bases = ", ".join(b.id for b in child.bases if isinstance(b, ast.Name))
                out.append(
                    f"{indent}class {child.name}({bases}):"
                    if bases
                    else f"{indent}class {child.name}:"
                )
                emit(child, indent + "    ")
            elif isinstance(child, ast.Assign) and indent == "":
                for target in child.targets:
                    if isinstance(target, ast.Name):
                        if assigns_emitted == max_assigns:
                            out.append("# [... more module constants elided]")
                        if assigns_emitted >= max_assigns:
                            assigns_emitted += 1
                            continue
                        out.append(f"{target.id} = ...")
                        assigns_emitted += 1

    emit(tree, "")
    return "\n".join(out)


def _pagerank(nodes, edges, personalization):
    """Personalized PageRank; pure-python, deterministic."""
    if not nodes:
        return {}
    total_p = sum(personalization.get(n, 1.0) for n in nodes)
    base = {n: personalization.get(n, 1.0) / total_p for n in nodes}
    scores = dict(base)
    out_edges = {n: sorted(t for t in edges.get(n, ()) if t in base) for n in nodes}
    ordered = sorted(nodes)
    for _ in range(_ITERATIONS):
        dangling_mass = sum(scores[n] for n in ordered if not out_edges[n])
        nxt = {
            n: (1.0 - _DAMPING) * base[n] + _DAMPING * dangling_mass * base[n]
            for n in ordered
        }
        for n in ordered:
            outs = out_edges[n]
            if outs:
                share = _DAMPING * scores[n] / len(outs)
                for t in outs:
                    nxt[t] += share
        scores = nxt
    return scores


def seed_paths_from_text(text: str, known_paths) -> set:
    """Paths mentioned in free text, resolved against the scanned file set
    (exact normalized match first, then unique basename match)."""
    seeds = set()
    if not text:
        return seeds
    by_norm = {_norm(p): p for p in known_paths}
    by_base = {}
    for p in known_paths:
        by_base.setdefault(os.path.basename(_norm(p)), []).append(p)
    for mention in _PATH_MENTION_RE.findall(text):
        m = _norm(mention)
        if m in by_norm:
            seeds.add(by_norm[m])
            continue
        candidates = by_base.get(os.path.basename(m), [])
        if len(candidates) == 1:
            seeds.add(candidates[0])
    return seeds


def build_budgeted_context(scanned_files, seeds=None, task_text="", budget=None):
    """Return a context dict (same path->content shape) trimmed to a token
    budget by graph relevance. Never raises; on any internal failure the
    input mapping is returned unchanged."""
    try:
        return _build(scanned_files, seeds, task_text, budget)
    except Exception:
        return scanned_files


def _build(scanned_files, seeds, task_text, budget):
    if not scanned_files:
        return scanned_files
    if budget is None:
        budget = _budget_from_env()
    if budget <= 0:
        return scanned_files

    tokens = {p: _count_tokens(c) for p, c in scanned_files.items()}
    if sum(tokens.values()) <= budget:
        # Identity passthrough: small workspaces behave exactly as before.
        return scanned_files

    paths = sorted(scanned_files)
    # --- graph over the python subset (stdlib ast) ---------------------
    infos = {}
    for p in paths:
        if p.endswith(".py"):
            try:
                infos[p] = _parse_py(scanned_files[p])
            except (SyntaxError, ValueError):
                infos[p] = (set(), set(), set())

    # Non-Python files join the graph when the optional tree-sitter layer is
    # installed; without it code_parse yields empty info and they stay
    # isolated nodes (the pre-R4 behavior, and the pure-Python test suite is
    # unaffected either way).
    ts_infos = {}
    for p in paths:
        if not p.endswith(".py"):
            parsed = code_parse.parse_source(p, scanned_files[p])
            if parsed["defs"] or parsed["refs"] or parsed["imports"]:
                ts_infos[p] = parsed

    module_index = {}
    for p in sorted(infos):
        for name in _module_names(p):
            module_index.setdefault(name, p)
    def_index = {}
    for p in sorted(infos):
        for d in infos[p][0]:
            def_index.setdefault(d, set()).add(p)
    for p in sorted(ts_infos):
        for name, _kind, _line, _sig in ts_infos[p]["defs"]:
            def_index.setdefault(name, set()).add(p)

    edges = {p: set() for p in paths}
    for p, (defs, refs, imports) in infos.items():
        for mod in imports:
            target = module_index.get(mod)
            if target and target != p:
                edges[p].add(target)
        for ref in refs:
            for target in def_index.get(ref, ()):
                if target != p:
                    edges[p].add(target)
    for p in sorted(ts_infos):
        parsed = ts_infos[p]
        for spec in parsed["imports"]:
            for target in code_parse.resolve_import(p, spec, paths):
                if target != p:
                    edges[p].add(target)
        for ref in parsed["refs"]:
            for target in def_index.get(ref, ()):
                if target != p:
                    edges[p].add(target)

    # --- personalization -----------------------------------------------
    seed_set = set()
    for s in seeds or ():
        matched = seed_paths_from_text(str(s), paths)
        if matched:
            seed_set |= matched
        elif s in scanned_files:
            seed_set.add(s)
    seed_set |= seed_paths_from_text(task_text, paths)

    task_idents = set(_IDENT_RE.findall(task_text or ""))
    mention_files = set()
    for ident in task_idents:
        mention_files |= def_index.get(ident, set())

    personalization = {}
    for p in paths:
        if p in seed_set:
            personalization[p] = _SEED_BOOST
        elif p in mention_files:
            personalization[p] = _MENTION_BOOST
        else:
            personalization[p] = 1.0

    scores = _pagerank(paths, edges, personalization)
    ordered = sorted(paths, key=lambda p: (-scores.get(p, 0.0), p))

    # --- spend the budget in rank order ---------------------------------
    result = {}
    remaining = budget
    for p in sorted(seed_set):
        result[p] = scanned_files[p]
        remaining -= tokens[p]
    for p in ordered:
        if p in result:
            continue
        if tokens[p] <= remaining:
            result[p] = scanned_files[p]
            remaining -= tokens[p]
        elif p.endswith(".py") and remaining > 0:
            skeleton = "# [context budget: signatures only]\n" + _skeleton(
                scanned_files[p]
            )
            skel_tokens = _count_tokens(skeleton)
            if skel_tokens <= remaining:
                result[p] = skeleton
                remaining -= skel_tokens
    return result
