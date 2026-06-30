import os
import re
import asyncio
import logging
from typing import List, Optional, Union
from ag_core.config import load_config

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
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "remote",
                "get-url",
                "origin",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                return stdout.decode().strip()
        except Exception:
            pass
        return None

    async def _get_current_branch(self, cwd: Optional[str]) -> Optional[str]:
        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                "rev-parse",
                "--abbrev-ref",
                "HEAD",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
            )
            stdout, _ = await proc.communicate()
            if proc.returncode == 0:
                return stdout.decode().strip()
        except Exception:
            pass
        return None

    async def _run_git(
        self, args: List[str], cwd: Optional[str] = None, env: Optional[dict] = None
    ) -> str:
        masked_cmd = "git " + " ".join(self._mask(arg) for arg in args)
        logger.info(f"Running command: {masked_cmd}")

        try:
            proc = await asyncio.create_subprocess_exec(
                "git",
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=cwd,
                env=env,
            )
            stdout_bytes, stderr_bytes = await proc.communicate()
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
        auth_url = self._get_auth_url(repo_url)
        return await self._run_git(["clone", auth_url, target_path])

    async def status(self, cwd: Optional[str] = None) -> str:
        return await self._run_git(["status"], cwd=cwd)

    async def add(self, files: Union[str, List[str]], cwd: Optional[str] = None) -> str:
        if isinstance(files, str):
            args = ["add", files]
        else:
            args = ["add"] + list(files)
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
            auth_url = self._get_auth_url(remote_url)
            args.append(auth_url)
            branch = await self._get_current_branch(cwd)
            if branch and branch != "HEAD":
                args.append(branch)
        return await self._run_git(args, cwd=cwd)

    async def push(self, cwd: Optional[str] = None) -> str:
        remote_url = await self._get_remote_url(cwd)
        args = ["push"]
        if remote_url:
            auth_url = self._get_auth_url(remote_url)
            args.append(auth_url)
            branch = await self._get_current_branch(cwd)
            if branch and branch != "HEAD":
                args.append(branch)
        return await self._run_git(args, cwd=cwd)
