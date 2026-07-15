"""Scan-root safety guard (ag_core/scanner/project_scanner.py).

A cwd-defaulting agent whose process was launched with no working directory
(e.g. an MCP server the IDE spawns with cwd="/") used to walk the ENTIRE
filesystem: os.walk("/") opened an unbounded number of files, ballooned memory
(~4.6 GB in a real Antigravity run), leaked out-of-project file contents into
the prompt, and never returned — the scan hung BEFORE context budgeting could
trim anything. ProjectScanner.scan() now refuses a filesystem-root / home
scan root BEFORE any os.walk, and resolve_workspace_root() lets the single-
agent MCP path pin a workspace via env instead of silently trusting cwd.
"""

import os

import pytest

from ag_core.scanner import project_scanner as ps
from ag_core.scanner.project_scanner import (
    ProjectScanner,
    assert_safe_scan_root,
    resolve_workspace_root,
)

_ENV_VARS = (
    "GENIUS_MCP_WORKSPACE",
    "GENIUS_WORKSPACE",
    "GENIUS_ALLOW_UNSAFE_SCAN_ROOT",
)


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in _ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# --- assert_safe_scan_root ----------------------------------------------------


def test_filesystem_root_is_rejected():
    with pytest.raises(ValueError) as exc:
        assert_safe_scan_root(os.path.abspath(os.sep))
    msg = str(exc.value)
    assert "filesystem root" in msg
    assert "GENIUS_MCP_WORKSPACE" in msg


def test_home_directory_is_rejected():
    with pytest.raises(ValueError, match="home directory"):
        assert_safe_scan_root(os.path.expanduser("~"))


def test_home_container_is_rejected():
    # e.g. /Users (macOS) or /home (Linux) — the parent of the home dir.
    container = os.path.dirname(os.path.abspath(os.path.expanduser("~")))
    with pytest.raises(ValueError):
        assert_safe_scan_root(container)


def test_real_project_dir_is_allowed(tmp_path):
    # A normal deep project directory passes silently.
    assert_safe_scan_root(str(tmp_path)) is None


def test_escape_hatch_allows_unsafe_root(monkeypatch):
    monkeypatch.setenv("GENIUS_ALLOW_UNSAFE_SCAN_ROOT", "1")
    # No raise even for the filesystem root.
    assert assert_safe_scan_root(os.path.abspath(os.sep)) is None


def test_symlink_to_root_is_rejected(tmp_path):
    # A symlink whose target is "/" must not slip the guard: os.walk would
    # follow the symlinked top into the real root. Reachable via an
    # explicitly-set GENIUS_MCP_WORKSPACE/GENIUS_WORKSPACE.
    link = tmp_path / "rootlink"
    os.symlink(os.path.abspath(os.sep), str(link))
    with pytest.raises(ValueError, match="filesystem root"):
        assert_safe_scan_root(str(link))


# --- the guard fires BEFORE os.walk (the actual hang) -------------------------


def test_scan_of_root_never_calls_os_walk(monkeypatch):
    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("os.walk must NOT be reached for a filesystem root")

    monkeypatch.setattr(ps.os, "walk", _boom)
    scanner = ProjectScanner(root_dir=os.path.abspath(os.sep))
    with pytest.raises(ValueError):
        scanner.scan()


def test_scan_of_real_dir_still_works(tmp_path):
    (tmp_path / "a.py").write_text("x = 1\n", encoding="utf-8")
    scanner = ProjectScanner(root_dir=str(tmp_path))
    out = scanner.scan()
    assert "a.py" in out and out["a.py"].strip() == "x = 1"


# --- resolve_workspace_root ---------------------------------------------------


def test_resolve_defaults_to_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    assert resolve_workspace_root() == str(tmp_path)


def test_resolve_prefers_mcp_workspace_env(monkeypatch, tmp_path):
    ws = tmp_path / "proj"
    ws.mkdir()
    monkeypatch.setenv("GENIUS_MCP_WORKSPACE", str(ws))
    assert resolve_workspace_root() == str(ws)


def test_resolve_generic_workspace_env_alone(monkeypatch, tmp_path):
    ws = tmp_path / "generic-only"
    ws.mkdir()
    monkeypatch.setenv("GENIUS_WORKSPACE", str(ws))
    assert resolve_workspace_root() == str(ws)


def test_resolve_mcp_wins_over_generic(monkeypatch, tmp_path):
    a = tmp_path / "mcp"
    b = tmp_path / "generic"
    a.mkdir()
    b.mkdir()
    monkeypatch.setenv("GENIUS_MCP_WORKSPACE", str(a))
    monkeypatch.setenv("GENIUS_WORKSPACE", str(b))
    assert resolve_workspace_root() == str(a)


def test_resolve_ignores_nonexistent_env_dir(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GENIUS_MCP_WORKSPACE", str(tmp_path / "does-not-exist"))
    # Falls through to cwd rather than pinning a bogus path.
    assert resolve_workspace_root() == str(tmp_path)


# --- the eval MCP tool's parallel walk is guarded too -------------------------


def test_grader_collect_case_refuses_root(monkeypatch):
    # The eval tool's grader has its OWN os.walk (bounded to 100 files) that
    # used to bypass the scanner guard entirely — from a cwd="/" MCP server it
    # walked the disk and leaked out-of-project .py files into the judge
    # context. collect_case now fails fast through the same guard.
    from ag_core.eval import grader

    def _boom(*a, **k):  # pragma: no cover - must never run
        raise AssertionError("os.walk must NOT run for a filesystem root")

    monkeypatch.setattr(grader.os, "walk", _boom)
    with pytest.raises(ValueError, match="Refusing to scan"):
        grader.collect_case(os.path.abspath(os.sep))


def test_grader_collect_case_ok_on_real_workspace(tmp_path):
    from ag_core.eval import grader

    (tmp_path / "app.py").write_text("x = 1\n", encoding="utf-8")
    case = grader.collect_case(str(tmp_path), prompt="p")
    assert case["code_files"] == {"app.py": "x = 1\n"}


@pytest.mark.asyncio
async def test_eval_tool_default_workspace_honors_env(monkeypatch, tmp_path):
    # The MCP eval tool defaults its workspace through resolve_workspace_root,
    # so the GENIUS_MCP_WORKSPACE pin rescues a cwd="/" server here exactly
    # like it does for the agent scan path.
    import mcp_server

    ws = tmp_path / "graded"
    ws.mkdir()
    (ws / "design.md").write_text("# design\n", encoding="utf-8")
    monkeypatch.setenv("GENIUS_MCP_WORKSPACE", str(ws))
    monkeypatch.chdir(os.path.abspath(os.sep))

    import json

    out = json.loads(await mcp_server.dispatch_tool("eval", {"op": "grade"}))
    assert out.get("workspace") == str(ws)


# --- end-to-end via a cwd-defaulting agent -----------------------------------


@pytest.mark.asyncio
async def test_agent_scan_from_root_fails_fast(monkeypatch):
    # Simulate the MCP cwd="/" launch: no workspace env, cwd is the fs root.
    monkeypatch.chdir(os.path.abspath(os.sep))

    def _boom(*a, **k):  # pragma: no cover
        raise AssertionError("os.walk must NOT run from the filesystem root")

    monkeypatch.setattr(ps.os, "walk", _boom)

    from ag_core.interfaces.base_agent import BaseAgent

    class _Agent(BaseAgent):
        async def run(self, prompt=None, context_data=None, *, effort=None):
            return ""

    agent = _Agent(name="guard-test", provider=object(), use_memory=False)
    with pytest.raises(ValueError, match="Refusing to scan"):
        agent.scan_context(None, task_text="anything")
