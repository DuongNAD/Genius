"""GIT_ASKPASS credential bridge (ag_core.utils.git).

The token must never appear in git's argv (visible to `ps`/procfs): push,
pull and clone against http(s) remotes pass a credential-stripped URL plus a
`-c credential.helper=` reset, and the credentials ride the subprocess env
consumed by a generated askpass script that itself contains no secrets.
Non-http(s) remotes (ssh, local paths) keep the plain args and inherit env.
"""

import os
import shutil
import subprocess

import pytest

from ag_core.utils import git as git_mod
from ag_core.utils.git import GitManager

TOKEN = "sup3r-secret-token"


@pytest.fixture
def git():
    return GitManager(username="john", token=TOKEN)


@pytest.fixture
def run_capture(git, monkeypatch):
    """Capture the (args, env) each high-level operation hands _run_git."""
    calls = []

    async def fake_run_git(args, cwd=None, env=None):
        calls.append({"args": list(args), "env": env})
        return ""

    monkeypatch.setattr(git, "_run_git", fake_run_git)
    return calls


def _remote(monkeypatch, git, url):
    async def fake_remote(cwd):
        return url

    async def fake_branch(cwd):
        return "main"

    monkeypatch.setattr(git, "_get_remote_url", fake_remote)
    monkeypatch.setattr(git, "_get_current_branch", fake_branch)


@pytest.mark.asyncio
async def test_push_keeps_the_token_out_of_argv(git, run_capture, monkeypatch):
    _remote(monkeypatch, git, "https://github.com/foo/bar.git")
    await git.push(cwd=".")
    (call,) = run_capture
    joined = " ".join(call["args"])
    assert TOKEN not in joined
    assert "john" not in joined
    # The credential machinery is reset to askpass-only for this call.
    assert call["args"][:2] == ["-c", "credential.helper="]
    assert "https://github.com/foo/bar.git" in call["args"]
    assert call["env"]["GENIUS_GIT_ASKPASS_PASSWORD"] == TOKEN
    assert os.path.isfile(call["env"]["GIT_ASKPASS"])


@pytest.mark.asyncio
async def test_pull_strips_credentials_already_embedded_in_the_remote(
    git, run_capture, monkeypatch
):
    _remote(monkeypatch, git, "https://olduser:oldpass@github.com/foo/bar.git")
    await git.pull(cwd=".")
    (call,) = run_capture
    joined = " ".join(call["args"])
    assert "oldpass" not in joined
    assert "https://github.com/foo/bar.git" in call["args"]
    assert call["env"]["GENIUS_GIT_ASKPASS_USERNAME"] == "john"


@pytest.mark.asyncio
async def test_clone_uses_the_askpass_env(git, run_capture):
    await git.clone("https://github.com/foo/bar.git", "target-dir")
    (call,) = run_capture
    assert TOKEN not in " ".join(call["args"])
    assert call["args"][:2] == ["-c", "credential.helper="]
    assert call["env"]["GENIUS_GIT_ASKPASS_PASSWORD"] == TOKEN


@pytest.mark.asyncio
async def test_non_http_remotes_stay_untouched(git, run_capture, monkeypatch):
    _remote(monkeypatch, git, "git@github.com:foo/bar.git")
    await git.push(cwd=".")
    await git.clone("/tmp/local/repo", "target-dir")
    push_call, clone_call = run_capture
    assert push_call["args"][0] == "push"
    assert push_call["env"] is None
    assert clone_call["args"][0] == "clone"
    assert clone_call["env"] is None


def test_askpass_script_contains_no_secrets(git):
    env = git._auth_env("https://github.com/foo/bar.git")
    with open(env["GIT_ASKPASS"], "r", encoding="utf-8") as f:
        body = f.read()
    assert TOKEN not in body
    assert "GENIUS_GIT_ASKPASS_PASSWORD" in body
    # Idempotent: the helper is written once per process.
    assert git_mod._ensure_askpass_script() == env["GIT_ASKPASS"]


@pytest.mark.skipif(shutil.which("sh") is None, reason="requires a POSIX sh")
def test_askpass_script_answers_git_prompts():
    """Executed the way git does (through sh): the Username prompt gets the
    username, anything else — the Password prompt — gets the password."""
    path = git_mod._ensure_askpass_script()
    env = {
        **os.environ,
        "GENIUS_GIT_ASKPASS_USERNAME": "answered-user",
        "GENIUS_GIT_ASKPASS_PASSWORD": "answered-pass",
    }
    user = subprocess.run(
        ["sh", path, "Username for 'https://github.com':"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert user.stdout.strip() == "answered-user"
    password = subprocess.run(
        ["sh", path, "Password for 'https://answered-user@github.com':"],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert password.stdout.strip() == "answered-pass"
