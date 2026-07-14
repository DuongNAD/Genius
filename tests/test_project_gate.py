"""Stack-aware project gates (GENIUS_PROJECT_GATE) — v1: npm.

Always off under pytest (same construction as auto-install), so these tests
drive the pieces directly with the subprocess layer faked — the suite must
never run npm or hit the network.
"""

import asyncio
import json
import os
import shutil
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orchestrator  # noqa: E402
from orchestrator import (  # noqa: E402
    _run_project_gates,
    detect_project_gates,
    project_gate_enabled,
)


def _write_pkg(tmp_path, scripts):
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "demo", "scripts": scripts}), encoding="utf-8"
    )


def test_project_gate_always_off_under_pytest(monkeypatch):
    monkeypatch.setenv("GENIUS_PROJECT_GATE", "1")
    assert project_gate_enabled() is False


def test_project_gate_env_parsing(monkeypatch):
    monkeypatch.setattr(orchestrator, "under_pytest", lambda: False)
    monkeypatch.delenv("GENIUS_PROJECT_GATE", raising=False)
    assert project_gate_enabled() is False
    monkeypatch.setenv("GENIUS_PROJECT_GATE", "1")
    assert project_gate_enabled() is True


def test_detect_no_manifest_is_empty(tmp_path):
    assert detect_project_gates(str(tmp_path)) == []


def test_detect_requires_npm_on_path(tmp_path, monkeypatch):
    _write_pkg(tmp_path, {"test": "vitest run"})
    monkeypatch.setattr(shutil, "which", lambda _: None)
    assert detect_project_gates(str(tmp_path)) == []


def test_detect_orders_install_then_declared_scripts(tmp_path, monkeypatch):
    _write_pkg(tmp_path, {"build": "next build", "test": "vitest run"})
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/npm")
    gates = detect_project_gates(str(tmp_path))
    names = [g[0] for g in gates]
    # install always first; only DECLARED scripts follow (no lint here).
    assert names == ["npm install", "npm run test", "npm run build"]
    assert gates[0][1][0].endswith("npm") or gates[0][1][0] == "cmd.exe"
    assert "--no-audit" in gates[0][1]
    assert all(isinstance(g[2], float) for g in gates)


def test_detect_bad_package_json_is_soft(tmp_path, monkeypatch):
    (tmp_path / "package.json").write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/npm")
    assert detect_project_gates(str(tmp_path)) == []


class GateRecorder:
    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    async def __call__(self, cmd, env=None, cwd=None, timeout=None):
        self.calls.append({"cmd": cmd, "env": env, "cwd": cwd})
        return self.results.pop(0)


@pytest.fixture()
def npm_project(tmp_path, monkeypatch):
    _write_pkg(tmp_path, {"test": "vitest run", "lint": "next lint"})
    monkeypatch.setattr(shutil, "which", lambda _: "/usr/bin/npm")
    return tmp_path


def test_gates_all_green(npm_project, monkeypatch):
    fake = GateRecorder([(0, "ok")] * 3)
    monkeypatch.setattr(orchestrator, "run_subprocess", fake)
    failed, section = asyncio.run(_run_project_gates(str(npm_project)))
    assert failed is False
    assert "### npm install" in section
    assert "### npm run test" in section and "### npm run lint" in section
    # CI env kills watch mode / interactive wizards; cwd is the project root.
    assert all(c["env"]["CI"] == "1" for c in fake.calls)
    assert all(c["cwd"] == str(npm_project) for c in fake.calls)


def test_gates_install_failure_short_circuits(npm_project, monkeypatch):
    fake = GateRecorder([(1, "EAI_AGAIN registry")])
    monkeypatch.setattr(orchestrator, "run_subprocess", fake)
    failed, section = asyncio.run(_run_project_gates(str(npm_project)))
    assert failed is True
    assert len(fake.calls) == 1
    assert "remaining gates skipped" in section


def test_gates_test_failure_marks_failed_but_continues(npm_project, monkeypatch):
    fake = GateRecorder([(0, "ok"), (1, "15 failed"), (0, "clean")])
    monkeypatch.setattr(orchestrator, "run_subprocess", fake)
    failed, section = asyncio.run(_run_project_gates(str(npm_project)))
    assert failed is True
    assert len(fake.calls) == 3  # lint still runs after a test failure
    assert "exit code: 1" in section


def test_gates_empty_when_no_manifest(tmp_path):
    failed, section = asyncio.run(_run_project_gates(str(tmp_path)))
    assert (failed, section) == (False, "")
