"""GitManager remote-URL validation.

Option injection (a URL beginning with ``-`` would parse as e.g.
``--upload-pack=...``) and git transport-helper URLs (``ext::`` executes
arbitrary commands) are refused BEFORE any git subprocess runs — including
remotes read back from a repo's own config on pull/push, which are
attacker-influenced when operating on cloned or generated repos. Ordinary
remotes — https, ssh, scp-like, plain local paths — stay allowed.
"""

import pytest

from ag_core.utils.git import GitError, GitManager


@pytest.fixture
def git():
    return GitManager(username="u", token="t")


def test_normal_remotes_are_allowed(git):
    for url in (
        "https://github.com/foo/bar.git",
        "http://example.com/repo",
        "git@github.com:foo/bar.git",
        "ssh://git@host/repo.git",
        "/tmp/local/repo",
        "../relative/repo.git",
        "file:///tmp/repo",
    ):
        assert git._validate_remote_url(url) == url


def test_option_injection_is_refused(git):
    for url in ("--upload-pack=/bin/evil", "-o=x", "  --config=x"):
        with pytest.raises(GitError):
            git._validate_remote_url(url)


def test_transport_helper_urls_are_refused(git):
    for url in ("ext::sh -c evil", "fd::17", "foo-bar::anything"):
        with pytest.raises(GitError):
            git._validate_remote_url(url)


def test_empty_url_is_refused(git):
    with pytest.raises(GitError):
        git._validate_remote_url("")


@pytest.mark.asyncio
async def test_clone_refuses_hostile_url_before_subprocess(git):
    with pytest.raises(GitError):
        await git.clone("ext::sh -c evil", "target-dir")
    with pytest.raises(GitError):
        await git.clone("--upload-pack=/bin/evil", "target-dir")


@pytest.mark.asyncio
async def test_pull_refuses_hostile_remote_from_repo_config(git, monkeypatch):
    async def hostile_remote(cwd):
        return "ext::sh -c evil"

    monkeypatch.setattr(git, "_get_remote_url", hostile_remote)
    with pytest.raises(GitError):
        await git.pull(cwd=".")


@pytest.mark.asyncio
async def test_push_refuses_option_remote_from_repo_config(git, monkeypatch):
    async def hostile_remote(cwd):
        return "--upload-pack=/bin/evil"

    monkeypatch.setattr(git, "_get_remote_url", hostile_remote)
    with pytest.raises(GitError):
        await git.push(cwd=".")
