import os
import tempfile
import pytest
import subprocess
from ag_core.utils.git import GitManager, GitError


def test_auth_env_construction():
    """Credentials travel in the subprocess env (GIT_ASKPASS bridge), never
    in the URL/argv. The username/password split mirrors the retired URL
    forms: user+token, token-as-username (PAT form), user with empty
    password."""
    git = GitManager(username="john", token="secret123")
    env = git._auth_env("https://github.com/foo/bar.git")
    assert os.path.isfile(env["GIT_ASKPASS"])
    assert env["GENIUS_GIT_ASKPASS_USERNAME"] == "john"
    assert env["GENIUS_GIT_ASKPASS_PASSWORD"] == "secret123"

    # Non-http(s) remotes and credential-less managers inject nothing.
    assert git._auth_env("git@github.com:foo/bar.git") is None
    assert GitManager(username="", token="")._auth_env("https://x/y.git") is None

    token_only = GitManager(username="", token="secret123")
    env = token_only._auth_env("https://github.com/foo/bar.git")
    assert env["GENIUS_GIT_ASKPASS_USERNAME"] == "secret123"
    assert env["GENIUS_GIT_ASKPASS_PASSWORD"] == ""

    user_only = GitManager(username="john", token="")
    env = user_only._auth_env("https://github.com/foo/bar.git")
    assert env["GENIUS_GIT_ASKPASS_USERNAME"] == "john"
    assert env["GENIUS_GIT_ASKPASS_PASSWORD"] == ""


def test_strip_url_credentials():
    git = GitManager(username="john", token="secret123")
    assert (
        git._strip_url_credentials("https://olduser:oldpass@github.com/foo/bar.git")
        == "https://github.com/foo/bar.git"
    )
    assert (
        git._strip_url_credentials("https://sometoken@github.com/foo/bar.git")
        == "https://github.com/foo/bar.git"
    )
    assert (
        git._strip_url_credentials("https://github.com/foo/bar.git")
        == "https://github.com/foo/bar.git"
    )


def test_credentials_masking():
    git = GitManager(username="john", token="secret123")
    assert (
        git._mask("https://john:secret123@github.com/foo/bar.git")
        == "https://***:***@github.com/foo/bar.git"
    )
    assert (
        git._mask("https://secret123@github.com/foo/bar.git")
        == "https://***@github.com/foo/bar.git"
    )
    assert (
        git._mask("Failed to authenticate token secret123 on remote")
        == "Failed to authenticate token *** on remote"
    )

    err = GitError(
        git._mask(
            "Authentication failed for https://john:secret123@github.com/foo/bar.git"
        )
    )
    assert "secret123" not in str(err)
    assert "***:***" in str(err)


@pytest.mark.asyncio
async def test_e2e_local_git_flow():
    with tempfile.TemporaryDirectory() as temp_dir:
        bare_repo_dir = os.path.join(temp_dir, "bare_repo.git")
        clone1_dir = os.path.join(temp_dir, "clone1")
        clone2_dir = os.path.join(temp_dir, "clone2")

        # Init bare repo
        subprocess.run(
            ["git", "init", "--bare", bare_repo_dir], check=True, capture_output=True
        )

        git = GitManager(username="testuser", token="testtoken")

        # Clone bare repo to clone1
        await git.clone(bare_repo_dir, clone1_dir)
        assert os.path.exists(clone1_dir)
        assert os.path.exists(os.path.join(clone1_dir, ".git"))

        # Add file
        test_file = os.path.join(clone1_dir, "test.txt")
        with open(test_file, "w") as f:
            f.write("hello git")

        # Status
        status_out = await git.status(cwd=clone1_dir)
        assert "test.txt" in status_out

        # Add
        await git.add("test.txt", cwd=clone1_dir)

        # Commit
        await git.commit(
            "initial commit",
            cwd=clone1_dir,
            author_name="Test Author",
            author_email="author@test.com",
        )

        # Push
        await git.push(cwd=clone1_dir)

        # Clone to clone2
        await git.clone(bare_repo_dir, clone2_dir)
        assert os.path.exists(clone2_dir)

        # Verify test.txt exists in clone2
        test_file_clone2 = os.path.join(clone2_dir, "test.txt")
        assert os.path.exists(test_file_clone2)
        with open(test_file_clone2, "r") as f:
            assert f.read() == "hello git"

        # Make a change in clone1
        with open(test_file, "a") as f:
            f.write("\nline 2")
        await git.add("test.txt", cwd=clone1_dir)
        await git.commit(
            "second commit",
            cwd=clone1_dir,
            author_name="Test Author",
            author_email="author@test.com",
        )
        await git.push(cwd=clone1_dir)

        # Pull in clone2
        await git.pull(cwd=clone2_dir)

        # Verify changes pulled
        with open(test_file_clone2, "r") as f:
            content = f.read()
            assert "line 2" in content
