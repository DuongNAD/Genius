import os
import sys
import shutil
import json
import asyncio
from typing import Any, Dict

from ag_core.interfaces.base_provider import BaseProvider, ProviderResponse, TokenUsage
from ag_core.utils.cli_resolver import which_external


class GrokProvider(BaseProvider):
    """
    Grok API (xAI) provider implementation using the local grok CLI.
    """

    def __init__(
        self,
        model_name: str = "grok-build-0.1",
        api_key: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        api_key = api_key or os.getenv("GROK_API_KEY") or os.getenv("XAI_API_KEY")
        base_url = base_url or os.getenv("GROK_BASE_URL") or "https://api.x.ai/v1"
        super().__init__(
            model_name=model_name, api_key=api_key, base_url=base_url, **kwargs
        )

    async def send_prompt(
        self, prompt: str, system: str | None = None, **kwargs: Any
    ) -> Dict[str, Any]:
        async with self.semaphore:
            await self.rate_limiter.acquire()

            extra = self.extra_params.copy()
            extra.update(kwargs)
            sys_prompt = extra.pop("system", None) or system

            cli_path = which_external("grok")
            if not cli_path:
                # Official xAI Grok Build CLI installs to ~/.grok/bin (added to
                # PATH on install, but a long-running process may predate that).
                userprofile = os.getenv("USERPROFILE") or os.path.expanduser("~")
                for name in ("grok.exe", "grok"):
                    candidate = os.path.join(userprofile, ".grok", "bin", name)
                    if os.path.exists(candidate):
                        cli_path = candidate
                        break
            if not cli_path:
                appdata = os.getenv("APPDATA")
                if appdata:
                    fallback = os.path.join(appdata, "npm", "grok.cmd")
                    if os.path.exists(fallback):
                        cli_path = fallback
            if not cli_path:
                userprofile = os.getenv("USERPROFILE")
                if userprofile:
                    fallback = os.path.join(
                        userprofile, "AppData", "Roaming", "npm", "grok.cmd"
                    )
                    if os.path.exists(fallback):
                        cli_path = fallback
            if not cli_path:
                cli_path = "grok"

            if not self.api_key:
                try:
                    login_cmd = [cli_path, "login"]
                    if sys.platform == "win32":
                        resolved_cli = shutil.which(cli_path) or cli_path
                        if resolved_cli.lower().endswith((".cmd", ".bat")):
                            login_cmd = ["cmd.exe", "/c"] + login_cmd
                    try:
                        login_process = await asyncio.create_subprocess_exec(
                            *login_cmd,
                            stdin=asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                    except OSError:
                        if sys.platform == "win32" and login_cmd[0] != "cmd.exe":
                            login_cmd = ["cmd.exe", "/c"] + login_cmd
                            login_process = await asyncio.create_subprocess_exec(
                                *login_cmd,
                                stdin=asyncio.subprocess.DEVNULL,
                                stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.PIPE,
                            )
                        else:
                            raise
                    await login_process.communicate()
                except Exception:
                    pass

            import tempfile

            temp_file_path = None
            try:
                if len(prompt) > 1000:
                    with tempfile.NamedTemporaryFile(
                        mode="w", suffix=".txt", delete=False, encoding="utf-8"
                    ) as f:
                        f.write(prompt)
                        temp_file_path = f.name
                    cmd = [
                        cli_path,
                        "--prompt-file",
                        temp_file_path,
                        "--output-format",
                        "json",
                    ]
                else:
                    cmd = [cli_path, "-p", prompt, "--output-format", "json"]

                session_id = (
                    extra.pop("session_id", None)
                    or kwargs.get("session_id")
                    or self.extra_params.get("session_id")
                )
                if session_id:
                    cmd.extend(["--session-id", str(session_id)])

                if sys_prompt:
                    cmd.extend(["--system-prompt-override", sys_prompt])

                actual_cmd = cmd
                if sys.platform == "win32":
                    resolved_cli = shutil.which(cli_path) or cli_path
                    if resolved_cli.lower().endswith((".cmd", ".bat")):
                        actual_cmd = ["cmd.exe", "/c"] + cmd

                try:
                    process = await asyncio.create_subprocess_exec(
                        *actual_cmd,
                        stdin=asyncio.subprocess.DEVNULL,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                except OSError:
                    if sys.platform == "win32" and actual_cmd == cmd:
                        actual_cmd = ["cmd.exe", "/c"] + cmd
                        process = await asyncio.create_subprocess_exec(
                            *actual_cmd,
                            stdin=asyncio.subprocess.DEVNULL,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                    else:
                        raise

                stdout, stderr = await process.communicate()
            finally:
                if temp_file_path and os.path.exists(temp_file_path):
                    try:
                        os.remove(temp_file_path)
                    except Exception:
                        pass

            if isinstance(process.returncode, int) and process.returncode != 0:
                stderr_str = stderr.decode("utf-8", errors="ignore").strip()
                raise RuntimeError(
                    f"Grok CLI failed with exit code {process.returncode}: {stderr_str}"
                )

            stdout_str = stdout.decode("utf-8", errors="ignore").strip()
            try:
                res_json = json.loads(stdout_str)
            except json.JSONDecodeError:
                res_json = {}

            content = res_json.get("result", "")
            prompt_tokens = res_json.get("usage", {}).get("input_tokens", 0)
            completion_tokens = res_json.get("usage", {}).get("output_tokens", 0)
            total_tokens = prompt_tokens + completion_tokens

            response = ProviderResponse(
                content=content,
                usage=TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                ),
            )
            return response.model_dump()
