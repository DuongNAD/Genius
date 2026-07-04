"""Queryable code-graph index (CodexGraph-lite).

CodexGraph (NAACL 2025) pairs an LLM agent with a graph database over the
repository; LocAgent shows the same structure queries work from a
lightweight in-memory graph. This is that lightweight variant for Genius:
one pass over a scanned workspace (stdlib ast + optional tree-sitter via
``code_parse``) builds def/import/reference indexes that answer the
structure queries agents actually issue — where is a symbol defined, who
references it, who imports this file, show a file's signature skeleton,
map the repo by relevance — with no database or service. Exposed to
callers through the MCP ``code_graph`` tool.
"""

import re

from ag_core.scanner import code_parse
from ag_core.scanner.repo_graph import (
    DEFAULT_TOKEN_BUDGET,
    _IDENT_RE,
    _MENTION_BOOST,
    _SEED_BOOST,
    _count_tokens,
    _module_names,
    _norm,
    _pagerank,
    _skeleton,
    seed_paths_from_text,
)


class RepoIndex:
    """Structural index over a scanned workspace (path -> content dict).

    Paths are normalized to posix-relative form; non-string contents are
    skipped (the scanner never produces them, but callers may).
    """

    def __init__(self, scanned_files):
        self.contents = {}
        self.parsed = {}
        for path in sorted(scanned_files or {}):
            content = scanned_files[path]
            if not isinstance(content, str):
                continue
            p = _norm(path)
            self.contents[p] = content
            self.parsed[p] = code_parse.parse_source(p, content)

        # symbol -> [{path, kind, line, signature}], insertion-ordered by path
        self.def_index = {}
        for p in sorted(self.parsed):
            for name, kind, line, sig in self.parsed[p]["defs"]:
                self.def_index.setdefault(name, []).append(
                    {"path": p, "kind": kind, "line": line, "signature": sig}
                )

        module_index = {}
        for p in sorted(self.parsed):
            for name in _module_names(p):
                module_index.setdefault(name, p)

        self.imports = {p: set() for p in self.parsed}
        for p in sorted(self.parsed):
            info = self.parsed[p]
            if info["lang"] == "python":
                for mod in info["imports"]:
                    target = module_index.get(mod)
                    if target and target != p:
                        self.imports[p].add(target)
            else:
                for spec in info["imports"]:
                    for target in code_parse.resolve_import(p, spec, self.parsed):
                        if target != p:
                            self.imports[p].add(target)

        self.importers = {p: set() for p in self.parsed}
        for p, targets in self.imports.items():
            for target in targets:
                self.importers[target].add(p)

    # --- queries --------------------------------------------------------

    def find_definition(self, symbol: str) -> list:
        """[{path, kind, line, signature}] for every definition of symbol."""
        return list(self.def_index.get(symbol, []))

    def find_references(self, symbol: str) -> list:
        """[{path, count}] for files whose text mentions symbol (word-bound),
        most references first."""
        if not symbol:
            return []
        pattern = re.compile(r"\b" + re.escape(symbol) + r"\b")
        out = []
        for p in sorted(self.contents):
            count = len(pattern.findall(self.contents[p]))
            if count:
                out.append({"path": p, "count": count})
        out.sort(key=lambda r: (-r["count"], r["path"]))
        return out

    def imports_of(self, path: str) -> list:
        """Repo files this file imports (resolved, sorted)."""
        return sorted(self.imports.get(_norm(path), ()))

    def importers_of(self, path: str) -> list:
        """Repo files that import this file (sorted)."""
        return sorted(self.importers.get(_norm(path), ()))

    def file_skeleton(self, path: str) -> str:
        """Signature-only rendering of one file ("" if unknown)."""
        p = _norm(path)
        content = self.contents.get(p)
        if content is None:
            return ""
        info = self.parsed[p]
        if info["lang"] == "python":
            return _skeleton(content)
        if info["defs"]:
            return "\n".join(sig or name for name, _kind, _line, sig in info["defs"])
        return "\n".join(content.splitlines()[:40])

    def repo_map(self, budget=None, task_text: str = "", seeds=None) -> str:
        """Aider-style ranked repo map: per-file signature skeletons emitted
        in personalized-PageRank order until the token budget is spent."""
        if budget is None or budget <= 0:
            budget = DEFAULT_TOKEN_BUDGET
        paths = sorted(self.parsed)
        if not paths:
            return ""

        edges = {p: set() for p in paths}
        for p in paths:
            edges[p] |= self.imports[p]
            for ref in self.parsed[p]["refs"]:
                for entry in self.def_index.get(ref, ()):
                    if entry["path"] != p:
                        edges[p].add(entry["path"])

        seed_set = set()
        for s in seeds or ():
            matched = seed_paths_from_text(str(s), paths)
            if matched:
                seed_set |= matched
            elif _norm(str(s)) in self.parsed:
                seed_set.add(_norm(str(s)))
        seed_set |= seed_paths_from_text(task_text, paths)

        mention_files = set()
        for ident in set(_IDENT_RE.findall(task_text or "")):
            for entry in self.def_index.get(ident, ()):
                mention_files.add(entry["path"])

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

        remaining = budget
        parts = []
        skipped = 0
        for p in ordered:
            block = f"--- {p} ---\n{self.file_skeleton(p)}\n"
            cost = _count_tokens(block)
            if cost <= remaining:
                parts.append(block)
                remaining -= cost
            else:
                skipped += 1
        if skipped:
            parts.append(f"# [{skipped} more files omitted by token budget]")
        return "\n".join(parts)


def build_index(scanned_files) -> RepoIndex:
    """Convenience constructor (mirrors build_budgeted_context's shape)."""
    return RepoIndex(scanned_files)
