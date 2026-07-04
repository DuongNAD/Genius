"""Tests for ag_core.scanner.code_parse + graph_index (R4 CodexGraph-lite)."""

import pytest

from ag_core.scanner import code_parse
from ag_core.scanner.graph_index import RepoIndex, build_index
from ag_core.scanner.repo_graph import _count_tokens, build_budgeted_context

ts_required = pytest.mark.skipif(
    not code_parse.TREE_SITTER_AVAILABLE, reason="tree-sitter not installed"
)

PY_MAIN = '''"""Main module."""
import util


def main():
    return util.helper()
'''
PY_UTIL = """def helper():
    return 42


class Box:
    def get(self):
        return 1
"""


def make_index():
    return RepoIndex(
        {"src/main.py": PY_MAIN, "src/util.py": PY_UTIL, "README.md": "# doc\n"}
    )


def test_find_definition_python():
    idx = make_index()
    hits = idx.find_definition("helper")
    assert hits == [
        {
            "path": "src/util.py",
            "kind": "function",
            "line": 1,
            "signature": "def helper():",
        }
    ]
    assert idx.find_definition("Box")[0]["kind"] == "class"
    assert idx.find_definition("nonexistent") == []


def test_imports_and_importers():
    idx = make_index()
    assert idx.imports_of("src/main.py") == ["src/util.py"]
    assert idx.importers_of("src/util.py") == ["src/main.py"]
    assert idx.importers_of("src/main.py") == []


def test_find_references_counts_text_occurrences():
    idx = make_index()
    refs = idx.find_references("helper")
    assert {r["path"] for r in refs} == {"src/main.py", "src/util.py"}
    assert all(r["count"] >= 1 for r in refs)
    assert idx.find_references("") == []


def test_file_skeleton_python():
    idx = make_index()
    skel = idx.file_skeleton("src/util.py")
    assert "def helper(): ..." in skel
    assert "class Box:" in skel
    assert "return 42" not in skel
    assert idx.file_skeleton("missing.py") == ""


def test_repo_map_ranks_and_respects_budget():
    idx = make_index()
    full_map = idx.repo_map(budget=10_000, task_text="fix helper in src/util.py")
    assert "--- src/util.py ---" in full_map
    assert "--- src/main.py ---" in full_map
    tiny_map = idx.repo_map(budget=25)
    assert "omitted by token budget" in tiny_map


def test_non_string_content_skipped():
    idx = build_index({"a.py": None, "b.py": "def f():\n    return 1\n"})
    assert idx.find_definition("f")
    assert "a.py" not in idx.contents


def test_python_parse_source_shape():
    info = code_parse.parse_source(
        "m.py", "import os\n\ndef f(a):\n    return os.path\n"
    )
    assert info["lang"] == "python"
    assert ("f", "function", 3, "def f(a):") in info["defs"]
    assert "os" in info["imports"]
    assert "os" in info["refs"]


def test_parse_source_never_raises():
    assert code_parse.parse_source("x.py", "def broken(:")["defs"] == []
    assert code_parse.parse_source("x.unknown", "whatever")["defs"] == []
    assert code_parse.parse_source("x.js", None)["defs"] == []
    assert code_parse.parse_source("", "text")["defs"] == []


JS_APP = (
    "import { helper } from './util';\n"
    "\n"
    "export function app() {\n"
    "  return helper(1);\n"
    "}\n"
)
JS_UTIL = (
    "export function helper(x) {\n" "  return x + 42;\n" "}\n" "const fmt = (x) => x;\n"
)
GO_MAIN = 'package main\n\nimport "myapp/pkg"\n\nfunc main() { pkg.Run() }\n'
GO_PKG = "package pkg\n\nfunc Run() int {\n\treturn 1\n}\n\ntype Server struct{}\n"


@ts_required
def test_javascript_defs_and_relative_import():
    idx = RepoIndex({"web/app.js": JS_APP, "web/util.js": JS_UTIL})
    hits = {d["path"]: d for d in idx.find_definition("helper")}
    assert "web/util.js" in hits
    assert hits["web/util.js"]["kind"] == "function"
    assert idx.imports_of("web/app.js") == ["web/util.js"]
    assert idx.importers_of("web/util.js") == ["web/app.js"]
    arrow = idx.find_definition("fmt")
    assert arrow and arrow[0]["kind"] == "function"
    skel = idx.file_skeleton("web/util.js")
    assert "helper" in skel


@ts_required
def test_go_defs_and_package_import():
    idx = RepoIndex({"cmd/main.go": GO_MAIN, "pkg/server.go": GO_PKG})
    assert idx.find_definition("Run")[0]["path"] == "pkg/server.go"
    assert idx.find_definition("Server")[0]["kind"] == "type"
    assert idx.imports_of("cmd/main.go") == ["pkg/server.go"]
    assert idx.importers_of("pkg/server.go") == ["cmd/main.go"]


@ts_required
def test_budgeted_context_gains_nonpython_edges():
    # Mirror of test_repo_graph's imported-file-outranks-isolated-file, in JS:
    # when only one of the two big files fits, it must be the imported one.
    hub = JS_UTIL + "".join(f"const pad_{i} = {i};\n" for i in range(400))
    loner = "export function lonely() { return 9; }\n" + "".join(
        f"const q_{i} = {i};\n" for i in range(400)
    )
    scanned = {
        "web/hub.js": hub,
        "web/loner.js": loner,
        "web/a.js": "import './hub';\nexport const A = 1;\n",
        "web/b.js": "import './hub';\nexport const B = 1;\n",
    }
    budget = _count_tokens(hub) + 200
    out = build_budgeted_context(scanned, budget=budget)
    assert out.get("web/hub.js") == hub
    assert out.get("web/loner.js") != loner
