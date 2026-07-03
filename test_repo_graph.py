"""Tests for ag_core.scanner.repo_graph (graph-aware budgeted context)."""

import ast

from ag_core.scanner.repo_graph import (
    DEFAULT_TOKEN_BUDGET,
    _count_tokens,
    _skeleton,
    build_budgeted_context,
    seed_paths_from_text,
)


def _make_module(n_lines: int, body: str = "") -> str:
    filler = "\n".join(f"x_{i} = {i}" for i in range(n_lines))
    return f"{body}\n{filler}\n"


def test_passthrough_identity_under_budget():
    scanned = {
        "a.py": "def a(): ...\n",
        "b.py": "def b(): ...\n",
        "README.md": "# hi\n",
    }
    out = build_budgeted_context(scanned, budget=10_000)
    # Small workspaces must see byte-identical behavior (same object).
    assert out is scanned


def test_zero_budget_disables_trimming():
    scanned = {"a.py": _make_module(2000)}
    out = build_budgeted_context(scanned, budget=0)
    assert out is scanned


def test_default_budget_from_env(monkeypatch):
    monkeypatch.delenv("GENIUS_CONTEXT_TOKEN_BUDGET", raising=False)
    scanned = {"a.py": "def a(): ...\n"}
    assert build_budgeted_context(scanned) is scanned
    monkeypatch.setenv("GENIUS_CONTEXT_TOKEN_BUDGET", "junk")
    assert build_budgeted_context(scanned) is scanned
    assert DEFAULT_TOKEN_BUDGET > 0


def test_seeds_kept_in_full_when_over_budget():
    big = _make_module(3000, "def target():\n    return 1")
    scanned = {
        "src/target.py": big,
        "src/other.py": _make_module(3000, "def other():\n    return 2"),
        "src/third.py": _make_module(3000, "def third():\n    return 3"),
    }
    out = build_budgeted_context(scanned, seeds=["src/target.py"], budget=4000)
    assert out["src/target.py"] == big  # seed survives verbatim
    # The others cannot all fit in full alongside the seed.
    full_others = [
        p for p in ("src/other.py", "src/third.py") if out.get(p) == scanned[p]
    ]
    assert len(full_others) < 2


def test_low_rank_files_become_signature_skeletons():
    scanned = {
        "used.py": _make_module(600, "def used():\n    return 1"),
        "caller.py": "import used\n\ndef caller():\n    return used.used()\n",
        "lonely.py": _make_module(
            600, 'def lonely(a, b):\n    """Lonely docstring."""\n    return a + b'
        ),
    }
    # Room for the seed (tiny) + one big file in full; the third must fall
    # back to a signature skeleton.
    budget = (
        _count_tokens(scanned["caller.py"]) + _count_tokens(scanned["used.py"]) + 200
    )
    out = build_budgeted_context(scanned, seeds=["caller.py"], budget=budget)
    assert out["caller.py"] == scanned["caller.py"]
    assert out["used.py"] == scanned["used.py"]  # imported by the seed -> full
    lonely = out.get("lonely.py", "")
    assert lonely.startswith("# [context budget: signatures only]")
    assert "def lonely(a, b): ..." in lonely
    assert "return a + b" not in lonely


def test_imported_file_outranks_isolated_file():
    # hub.py is imported by two files; loner.py by nobody. When only one of
    # the two big files can stay full, it must be hub.py.
    hub = _make_module(600, "def hub():\n    return 0")
    scanned = {
        "hub.py": hub,
        "loner.py": _make_module(600, "def loner():\n    return 9"),
        "user1.py": "import hub\n\ndef u1():\n    return hub.hub()\n",
        "user2.py": "import hub\n\ndef u2():\n    return hub.hub()\n",
    }
    budget = _count_tokens(hub) + 200
    out = build_budgeted_context(scanned, budget=budget)
    assert out.get("hub.py") == hub
    assert out.get("loner.py") != scanned["loner.py"]


def test_syntax_error_file_fails_soft():
    scanned = {
        "ok.py": _make_module(1500, "def ok():\n    return 1"),
        "broken.py": _make_module(1500, "def broken(:\n    ???"),
    }
    out = build_budgeted_context(scanned, budget=2000)
    assert isinstance(out, dict) and out


def test_deterministic_output():
    scanned = {
        f"m{i}.py": _make_module(400, f"def f{i}():\n    return {i}") for i in range(8)
    }
    a = build_budgeted_context(dict(scanned), budget=1500)
    b = build_budgeted_context(dict(scanned), budget=1500)
    assert a == b


def test_task_text_mentions_boost_file():
    scanned = {
        "alpha.py": _make_module(600, "def compute_alpha():\n    return 1"),
        "beta.py": _make_module(600, "def compute_beta():\n    return 2"),
        "gamma.py": _make_module(600, "def compute_gamma():\n    return 3"),
    }
    budget = _count_tokens(scanned["beta.py"]) + 200
    out = build_budgeted_context(
        scanned, task_text="Please fix compute_beta so it rounds up", budget=budget
    )
    assert out.get("beta.py") == scanned["beta.py"]


def test_seed_paths_from_text_resolution():
    known = ["src/app/main.py", "tests/test_main.py", "README.md"]
    seeds = seed_paths_from_text(
        "Implement src/app/main.py and cover it in test_main.py", known
    )
    assert seeds == {"src/app/main.py", "tests/test_main.py"}


def test_skeleton_renders_signatures():
    src = (
        '"""Module docstring line one.\nMore."""\n'
        "LIMIT = 10\n\n"
        "class Greeter(Base):\n"
        '    """Doc."""\n'
        "    def hello(self, name):\n"
        "        return f'hi {name}'\n\n"
        "async def run(loop):\n"
        "    return loop\n"
    )
    skel = _skeleton(src)
    assert '"""Module docstring line one."""' in skel
    assert "LIMIT = ..." in skel
    assert "class Greeter(Base):" in skel
    assert "    def hello(self, name): ..." in skel
    assert "async def run(loop): ..." in skel
    assert "hi {name}" not in skel
    # Skeleton must itself be valid-ish python structure (parses as module
    # when bodies are ellipses on def lines) — guard against broken emits.
    ast.parse(skel.replace(": ...", ": pass"))


def test_never_raises_returns_input_on_weird_values():
    scanned = {"a.py": None}  # content that breaks token counting
    out = build_budgeted_context(scanned, budget=10)
    assert out is scanned
