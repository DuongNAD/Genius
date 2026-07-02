import os
import sys
import asyncio
from typing import Any, Dict

from ag_core.interfaces.base_provider import BaseProvider, ProviderResponse, TokenUsage
from ag_core.utils.cli_resolver import which_external
from ag_core.utils.cli_runner import (
    communicate_with_timeout,
    explain_cli_failure,
    extract_json_object,
    tail_text,
)


def resolve_claude_cli() -> str:
    """Resolve the real Claude CLI path, never the bundled repo wrapper.

    Shared by send_prompt and the ``--doctor`` preflight. Raises
    :class:`RuntimeError` when no real CLI is found - a bare-name fallback
    would let ``shutil.which``/``cmd.exe`` resolve the repo's own
    ``claude.cmd`` wrapper again (the recursion bug).
    """
    cli_path = which_external("claude")
    if not cli_path:
        appdata = os.getenv("APPDATA")
        if appdata:
            fallback = os.path.join(appdata, "npm", "claude.cmd")
            if os.path.exists(fallback):
                cli_path = fallback
    if not cli_path:
        userprofile = os.getenv("USERPROFILE")
        if userprofile:
            fallback = os.path.join(
                userprofile, "AppData", "Roaming", "npm", "claude.cmd"
            )
            if os.path.exists(fallback):
                cli_path = fallback
    if not cli_path:
        if os.getenv("PYTEST_CURRENT_TEST"):
            # Unit tests stub the subprocess layer; keep a harmless literal so
            # they don't require a real install.
            return "claude"
        raise RuntimeError(
            "CLI 'claude' not found; install Claude Code (npm install -g "
            "@anthropic-ai/claude-code) or run `python serve.py --doctor` "
            "to diagnose."
        )
    return cli_path


class AnthropicProvider(BaseProvider):
    """
    Anthropic Claude API provider implementation using the local claude CLI.
    """

    def __init__(
        self,
        model_name: str = "claude-3-5-sonnet-20241022",
        api_key: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        base_url = (
            base_url
            or os.getenv("ANTHROPIC_BASE_URL")
            or "https://api.anthropic.com/v1"
        )
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

            cli_path = resolve_claude_cli()

            # The prompt is always fed via stdin: the CLI has no `--input
            # <file>` flag (every prompt >1000 chars used to fail with
            # "unknown option '--input'"), and inlining it as a `-p <prompt>`
            # argument goes through cmd.exe on Windows, which interprets
            # `&|<>^%` and newlines in LLM-generated prompt text.
            # `--tools` gets a real empty string (not the two-char literal
            # '""') which the CLI documents as "disable all tools".
            cmd = [
                cli_path,
                "-p",
                "--bare",
                "--tools",
                "",
                "--output-format",
                "json",
            ]

            if sys_prompt:
                cmd.extend(["--system-prompt", sys_prompt])

            # Wrap .cmd/.bat shims with cmd.exe /c, decided from the resolved
            # path itself - never via a raw shutil.which on a bare name, which
            # searches the cwd first and would re-introduce the repo-wrapper
            # recursion.
            actual_cmd = cmd
            if sys.platform == "win32" and cli_path.lower().endswith((".cmd", ".bat")):
                actual_cmd = ["cmd.exe", "/c"] + cmd

            try:
                process = await asyncio.create_subprocess_exec(
                    *actual_cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            except OSError:
                if sys.platform == "win32" and actual_cmd == cmd:
                    actual_cmd = ["cmd.exe", "/c"] + cmd
                    process = await asyncio.create_subprocess_exec(
                        *actual_cmd,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                    )
                else:
                    raise

            stdout, stderr = await communicate_with_timeout(
                process, input=prompt.encode("utf-8"), cli_name="Claude CLI"
            )

            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            if isinstance(process.returncode, int) and process.returncode != 0:
                raise RuntimeError(
                    explain_cli_failure(
                        "Claude CLI", process.returncode, stderr_str, stdout_str
                    )
                )

            res_json = extract_json_object(stdout_str) or {}

            # The JSON envelope reports failures inline: honor `is_error`
            # instead of returning its error text (or "") as a success.
            if res_json.get("is_error"):
                error_msg = str(res_json.get("result") or res_json.get("error") or "")
                raise RuntimeError(
                    f"Claude CLI reported an error: {error_msg} | "
                    f"stderr tail: {tail_text(stderr_str)}"
                )

            content = res_json.get("result", "")
            if not content:
                raise RuntimeError(
                    "Claude CLI produced no result (output was empty or not "
                    "the expected JSON envelope). "
                    f"stdout tail: {tail_text(stdout_str)} | "
                    f"stderr tail: {tail_text(stderr_str)}"
                )

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
