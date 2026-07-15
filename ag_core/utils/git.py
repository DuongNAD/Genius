import os
import re
import asyncio
import logging
from typing import List, Optional, Union
from ag_core.config import load_config
from ag_core.utils.cli_runner import (
    communicate_with_timeout,
    cli_timeout,
    CLITimeoutError,
)

logger = logging.getLogger("ag_core.utils.git")


class GitError(Exception):
    """Exception raised for errors in Git operations."""


class GitManager:
    def __init__(self, username: Optional[str] = None, token: Optional[str] = None):
        try:
            config = load_config()
            self.username = (
                username or config.git_username or os.getenv("GIT_USERNAME", "")
            )
            self.token = token or config.git_token or os.getenv("GIT_TOKEN", "")
        except Exception:
            self.username = username or os.getenv("GIT_USERNAME", "")
            self.token = token or os.getenv("GIT_TOKEN", "")

    def _mask(self, text: str) -> str:
        if not text:
            return text
        # Mask URL basic auth: https://user:pass@domain -> https://***:***@domain
        text = re.sub(r"(https?://)[^/:]+:[^/@]+@", r"\1***:***@", text)
        # Mask URL token auth: https://token@domain -> https://***@domain
        text = re.sub(r"(https?://)[^/:@]+@", r"\1***@", text)
        if self.token:
            text = text.replace(self.token, "***")
        return text

    # git's transport-helper syntax `<helper>::<address>` (ext::, fd::, ...)
    # executes commands / reads arbitrary descriptors — never a legitimate
    # remote here. Plain paths, file://, https://, ssh:// and scp-like
    # user@host:path remotes all stay allowed (local-path clones are a
    # supported case).
    _HELPER_URL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9+.-]*::")

    def _validate_remote_url(self, url: str) -> str:
        """Refuse remote URLs that could smuggle git options or transport
        helpers. Remotes read back from repo config are attacker-influenced
        when operating on cloned/generated repos: a leading '-' would parse
        as an option (e.g. --upload-pack=...), and ext:: runs commands."""
        cleaned = (url or "").strip()
        if not cleaned or cleaned.startswith("-"):
            raise GitError(f"Refusing unsafe git remote URL: {self._mask(cleaned)!r}")
        if self._HELPER_URL_RE.match(cleaned):
            raise GitError(
                f"Refusing git transport-helper remote URL: {self._mask(cleaned)!r}"
            )
        return cleaned

    def _get_auth_url(self, url: str) -> str:
        if not url:
            return url
        if not (url.startswith("https://") or url.startswith("http://")):
            return url
        if not self.username and not self.token:
            return url

        # Remove any existing credentials in the URL
        clean_url = re.sub(r"(https?://)[^/]+@", r"\1", url)

        if self.username and self.token:
            auth_part = f"{self.username}:{self.token}@"
        elif self.token:
            auth_part = f"{self.token}@"
        else:
            auth_part = f"{self.username}@"

        if clean_url.startswith("https://"):
            return clean_url.replace("https://", f"https://{auth_part}", 1)
        elif clean_url.startswith("http://"):
            return clean_url.replace("http://", f"http://{auth_part}", 1)
        return url

    async def _get_remote_url(self, cwd: Optional[str]) -> Optional[str]:
        # Through _run_git for the timeout backstop + prompt hardening
        # (GIT_TERMINAL_PROMPT=0, closed stdin): a wedged `git remote get-url`
        # (credential helper, lock file) must not hang the caller forever.
        try:
            out = await self._run_git(["remote", "get-url", "origin"], cwd=cwd)
            return out.strip() or None
        except GitError:
            return None

    async def _get_current_branch(self, cwd: Optional[str]) -> Optional[str]:
        try:
            out = await self._run_git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=cwd)
            return out.strip() or None
        except GitError:
            return None

    async def _run_git(
        self, args: List[str], cwd: Optional[str] = None, env: Optional[dict] = None
    ) -> str:
        masked_cmd = "git " + " ".join(self._mask(arg) for arg in args)
        logger.info(f"Running command: {masked_cmd}")

        # Never let git block on an interactive credential/passphrase prompt: a
        # missing or expired token on a push/pull/clone would otherwise hang the
        # coroutine forever. GIT_TERMINAL_PROMPT=0 plus a closed stdin makes auth
        # failures fail fast, and communicate_with_timeout is a hard backstop.
        run_env = dict(env) if env is not None else os.environ.copy()
        run_env.setdefault("GIT_TERMINAL_PROMPT", "0")
        run_env.setdefault("GCM_INTERACTIVE", "never")

        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=run_env,
            )
            try:
                stdout_bytes, stderr_bytes = await communicate_with_timeout(
                    proc, timeout=cli_timeout(), cli_name="git"
                )
            except CLITimeoutError as e:
                raise GitError(self._mask(str(e)))
            stdout = stdout_bytes.decode(errors="replace")
            stderr = stderr_bytes.decode(errors="replace")

            if proc.returncode != 0:
                error_msg = f"Git command failed: {masked_cmd}\nExit code: {proc.returncode}\nStdout: {stdout}\nStderr: {stderr}"
                raise GitError(self._mask(error_msg))

            return self._mask(stdout)
        except Exception as e:
            if isinstance(e, GitError):
                raise
            raise GitError(self._mask(f"Failed to execute git command: {str(e)}"))

    async def clone(self, repo_url: str, target_path: str) -> str:
        auth_url = self._get_auth_url(self._validate_remote_url(repo_url))
        # `--` so a repo_url beginning with `-` can't be parsed as a git option
        # (e.g. --upload-pack=...); it's forced to be positional.
        return await self._run_git(["clone", "--", auth_url, target_path])

    async def status(self, cwd: Optional[str] = None) -> str:
        return await self._run_git(["status"], cwd=cwd)

    async def add(self, files: Union[str, List[str]], cwd: Optional[str] = None) -> str:
        # `--` so a filename beginning with `-` isn't parsed as an option flag.
        if isinstance(files, str):
            args = ["add", "--", files]
        else:
            args = ["add", "--"] + list(files)
        return await self._run_git(args, cwd=cwd)

    async def commit(
        self,
        message: str,
        cwd: Optional[str] = None,
        author_name: Optional[str] = None,
        author_email: Optional[str] = None,
    ) -> str:
        env = os.environ.copy()
        if author_name:
            env["GIT_AUTHOR_NAME"] = author_name
            env["GIT_COMMITTER_NAME"] = author_name
        if author_email:
            env["GIT_AUTHOR_EMAIL"] = author_email
            env["GIT_COMMITTER_EMAIL"] = author_email
        return await self._run_git(["commit", "-m", message], cwd=cwd, env=env)

    async def pull(self, cwd: Optional[str] = None) -> str:
        remote_url = await self._get_remote_url(cwd)
        args = ["pull"]
        if remote_url:
            # The URL comes back from the repo's own config: validate it and
            # pin it positional with `--` so it can never parse as an option.
            auth_url = self._get_auth_url(self._validate_remote_url(remote_url))
            args.append("--")
            args.append(auth_url)
            branch = await self._get_current_branch(cwd)
            if branch and branch != "HEAD":
                args.append(branch)
        return await self._run_git(args, cwd=cwd)

    async def push(self, cwd: Optional[str] = None) -> str:
        remote_url = await self._get_remote_url(cwd)
        args = ["push"]
        if remote_url:
            # Same hardening as pull: repo-config data stays positional.
            auth_url = self._get_auth_url(self._validate_remote_url(remote_url))
            args.append("--")
            args.append(auth_url)
            branch = await self._get_current_branch(cwd)
            if branch and branch != "HEAD":
                args.append(branch)
        return await self._run_git(args, cwd=cwd)
