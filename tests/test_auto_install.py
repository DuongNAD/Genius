"""Dependency auto-install (GENIUS_AUTO_INSTALL) — orchestrator helpers.

The feature is opt-in and ALWAYS off under pytest (same convention as the
eval gate), so these tests exercise the pieces directly: the flag parsing,
the manifest predicate, the wave partition, the venv path resolution, and the
best-effort installer (with the subprocess layer faked — the suite must never
create a real venv or hit the network).
"""

import asyncio
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import orchestrator  # noqa: E402
from orchestrator import (  # noqa: E402
    auto_install_enabled,
    auto_install_requirements,
    is_dependency_manifest,
    partition_fanout_waves,
    project_venv_dir,
    venv_python,
    verification_python,
)


# ---------------------------------------------------------------- flag


def test_auto_install_always_off_under_pytest(monkeypatch):
    monkeypatch.setenv("GENIUS_AUTO_INSTALL", "1")
    assert auto_install_enabled() is False


@pytest.mark.parametrize(
    "raw,expected",
    [
        (None, False),
        ("", False),
        ("0", False),
        ("false", False),
        ("1", True),
        ("true", True),
        ("YES", True),
    ],
)
def test_auto_install_env_parsing(monkeypatch, raw, expected):
    monkeypatch.setattr(orchestrator, "under_pytest", lambda: False)
    if raw is None:
        monkeypatch.delenv("GENIUS_AUTO_INSTALL", raising=False)
    else:
        monkeypatch.setenv("GENIUS_AUTO_INSTALL", raw)
    assert auto_install_enabled() is expected


# ---------------------------------------------------------------- predicate


@pytest.mark.parametrize(
    "path",
    [
        "requirements.txt",
        "requirements-dev.txt",
        "requirements_ci.txt",
        "REQUIREMENTS.TXT",
    ],
)
def test_dependency_manifest_matches_root_requirements(path):
    assert is_dependency_manifest(path) is True


@pytest.mark.parametrize(
    "path",
    [
        "src/requirements.txt",
        "deploy\\requirements.txt",
        "/requirements.txt",
        "\\requirements.txt",
        "C:\\requirements.txt",
        "requirements.md",
        "requirements",
        "pyproject.toml",
        "test_requirements.txt.py",
        "",
        None,
    ],
)
def test_dependency_manifest_rejects_everything_else(path):
    assert is_dependency_manifest(path) is False


# ---------------------------------------------------------------- partition


FILES = [
    {"path": "app.py", "description": "impl"},
    {"path": "requirements.txt", "description": "deps"},
    {"path": "tests/test_app.py", "description": "designed test"},
]


def test_partition_default_keeps_manifest_in_impl_wave():
    manifests, impl, tests = partition_fanout_waves(FILES)
    assert manifests == []
    assert [f["path"] for f in impl] == ["app.py", "requirements.txt"]
    assert [f["path"] for f in tests] == ["tests/test_app.py"]


def test_partition_peels_manifest_wave_when_enabled(monkeypatch):
    monkeypatch.setattr(orchestrator, "auto_install_enabled", lambda: True)
    manifests, impl, tests = partition_fanout_waves(FILES)
    assert [f["path"] for f in manifests] == ["requirements.txt"]
    assert [f["path"] for f in impl] == ["app.py"]
    assert [f["path"] for f in tests] == ["tests/test_app.py"]


# ---------------------------------------------------------------- venv paths


def test_venv_lives_under_pipeline_internal_dir(tmp_path):
    project = tmp_path / "projects" / "demo"
    assert project_venv_dir(str(project)) == os.path.join(
        str(tmp_path), ".genius", "demo", "venv"
    )


def test_venv_python_is_platform_aware(tmp_path):
    py = venv_python(str(tmp_path / "proj"))
    if os.name == "nt":
        assert py.endswith(os.path.join("venv", "Scripts", "python.exe"))
    else:
        assert py.endswith(os.path.join("venv", "bin", "python"))


def _make_fake_venv(project_dir: str) -> str:
    py = venv_python(project_dir)
    os.makedirs(os.path.dirname(py), exist_ok=True)
    with open(py, "w", encoding="utf-8") as fh:
        fh.write("")
    return py


def test_verification_python_defaults_to_sys_executable(tmp_path):
    # Flag off (under pytest): even an existing venv must never hijack.
    project = str(tmp_path / "proj")
    _make_fake_venv(project)
    assert verification_python(project) == sys.executable


def test_verification_python_uses_venv_when_enabled(tmp_path, monkeypatch):
    monkeypatch.setattr(orchestrator, "auto_install_enabled", lambda: True)
    project = str(tmp_path / "proj")
    assert verification_python(project) == sys.executable  # not built yet
    py = _make_fake_venv(project)
    assert verification_python(project) == py


# ---------------------------------------------------------------- installer


class SubprocessRecorder:
    """Fake run_subprocess: records commands, scripts venv creation."""

    def __init__(self, project_dir, results=None):
        self.project_dir = project_dir
        self.calls = []
        self.results = list(results or [])

    async def __call__(self, cmd, env=None, cwd=None, timeout=None):
        self.calls.append({"cmd": cmd, "cwd": cwd, "timeout": timeout})
        code, out = (self.results.pop(0)) if self.results else (0, "ok")
        if code == 0 and "venv" in cmd and "-m" in cmd:
            _make_fake_venv(self.project_dir)
        return code, out


def test_installer_creates_venv_then_pip_installs(tmp_path, monkeypatch):
    project = str(tmp_path / "projects" / "demo")
    os.makedirs(project)
    manifest = os.path.join(project, "requirements.txt")
    with open(manifest, "w", encoding="utf-8") as fh:
        fh.write("httpx\n")

    fake = SubprocessRecorder(project)
    monkeypatch.setattr(orchestrator, "run_subprocess", fake)

    asyncio.run(auto_install_requirements(project, ["requirements.txt"]))

    assert len(fake.calls) == 2
    venv_cmd = fake.calls[0]["cmd"]
    assert venv_cmd[:3] == [sys.executable, "-m", "venv"]
    assert "--system-site-packages" in venv_cmd
    assert venv_cmd[-1] == project_venv_dir(project)

    pip_cmd = fake.calls[1]["cmd"]
    assert pip_cmd[0] == venv_python(project)
    assert pip_cmd[1:4] == ["-m", "pip", "install"]
    assert "--no-input" in pip_cmd
    assert pip_cmd[-2:] == ["-r", manifest]
    assert fake.calls[1]["cwd"] == project

    log_path = os.path.join(
        orchestrator.pipeline_internal_dir(project), "logs", "install.log"
    )
    with open(log_path, encoding="utf-8") as fh:
        log = fh.read()
    assert "pip install -r requirements.txt" in log


def test_installer_reuses_existing_venv(tmp_path, monkeypatch):
    project = str(tmp_path / "projects" / "demo")
    os.makedirs(project)
    _make_fake_venv(project)
    with open(os.path.join(project, "requirements.txt"), "w", encoding="utf-8") as fh:
        fh.write("httpx\n")

    fake = SubprocessRecorder(project)
    monkeypatch.setattr(orchestrator, "run_subprocess", fake)
    asyncio.run(auto_install_requirements(project, ["requirements.txt"]))

    assert len(fake.calls) == 1  # no venv creation, straight to pip
    assert fake.calls[0]["cmd"][1:4] == ["-m", "pip", "install"]


def test_installer_venv_failure_is_soft_and_skips_pip(tmp_path, monkeypatch):
    project = str(tmp_path / "projects" / "demo")
    os.makedirs(project)
    with open(os.path.join(project, "requirements.txt"), "w", encoding="utf-8") as fh:
        fh.write("httpx\n")

    fake = SubprocessRecorder(project, results=[(1, "boom")])
    monkeypatch.setattr(orchestrator, "run_subprocess", fake)
    asyncio.run(auto_install_requirements(project, ["requirements.txt"]))

    assert len(fake.calls) == 1  # only the failed venv attempt
    assert verification_python(project) == sys.executable


def test_installer_pip_failure_is_soft(tmp_path, monkeypatch):
    project = str(tmp_path / "projects" / "demo")
    os.makedirs(project)
    with open(os.path.join(project, "requirements.txt"), "w", encoding="utf-8") as fh:
        fh.write("no-such-package-xyz\n")

    fake = SubprocessRecorder(project, results=[(0, "created"), (1, "resolution")])
    monkeypatch.setattr(orchestrator, "run_subprocess", fake)
    # Must not raise: verification will surface the missing dep instead.
    asyncio.run(auto_install_requirements(project, ["requirements.txt"]))
    assert len(fake.calls) == 2


def test_installer_skips_never_written_manifest(tmp_path, monkeypatch):
    project = str(tmp_path / "projects" / "demo")
    os.makedirs(project)

    fake = SubprocessRecorder(project)
    monkeypatch.setattr(orchestrator, "run_subprocess", fake)
    asyncio.run(auto_install_requirements(project, ["requirements.txt"]))

    assert len(fake.calls) == 1  # venv creation only, no pip run
    log_path = os.path.join(
        orchestrator.pipeline_internal_dir(project), "logs", "install.log"
    )
    with open(log_path, encoding="utf-8") as fh:
        assert "skipped: file was never written" in fh.read()


def test_installer_rejects_absolute_manifest_without_reading_host(tmp_path, monkeypatch):
    project = str(tmp_path / "projects" / "demo")
    os.makedirs(project)
    fake = SubprocessRecorder(project)
    monkeypatch.setattr(orchestrator, "run_subprocess", fake)

    asyncio.run(auto_install_requirements(project, ["/requirements.txt"]))

    # The isolated venv may be created, but pip must never receive the host path.
    assert len(fake.calls) == 1
    assert "venv" in fake.calls[0]["cmd"]
    log_path = os.path.join(
        orchestrator.pipeline_internal_dir(project), "logs", "install.log"
    )
    with open(log_path, encoding="utf-8") as fh:
        assert "skipped: unsafe manifest path" in fh.read()


def test_installer_uses_install_timeout(tmp_path, monkeypatch):
    project = str(tmp_path / "projects" / "demo")
    os.makedirs(project)
    with open(os.path.join(project, "requirements.txt"), "w", encoding="utf-8") as fh:
        fh.write("httpx\n")
    monkeypatch.setenv("GENIUS_INSTALL_TIMEOUT", "123.5")

    fake = SubprocessRecorder(project)
    monkeypatch.setattr(orchestrator, "run_subprocess", fake)
    asyncio.run(auto_install_requirements(project, ["requirements.txt"]))
    assert all(call["timeout"] == 123.5 for call in fake.calls)
