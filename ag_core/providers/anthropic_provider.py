import os
import tempfile
from typing import Any, Dict

from ag_core.interfaces.base_provider import BaseProvider, ProviderResponse, TokenUsage
from ag_core.utils.cli_resolver import memoize_cli_path, which_external
from ag_core.utils.cli_runner import (
    communicate_with_timeout,
    explain_cli_failure,
    extract_json_object,
    spawn_cli,
    tail_text,
)


@memoize_cli_path
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
        model_name: str = "",
        api_key: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        # An empty model_name means "use the CLI's configured default model";
        # a value is passed through as `--model` (alias or full id).
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
            # No `--bare`: on Claude Code 2.1.x its minimal mode also skips
            # the stored OAuth credentials, so every call fails with "Not
            # logged in" even on a logged-in machine.
            cmd = [
                cli_path,
                "-p",
                "--tools",
                "",
                "--output-format",
                "json",
            ]

            if self.model_name:
                cmd.extend(["--model", self.model_name])

            if sys_prompt:
                # Never pass the system prompt as an argv element: the
                # cmd.exe /c wrapper mangles multi-line arguments (everything
                # after the first newline is silently lost), so a
                # --system-prompt argument delivered only its first line — a
                # real run dropped the architect's entire JSON output
                # contract. Prepend it to the stdin payload instead (same
                # pattern as the agy and codex providers).
                prompt = f"{sys_prompt}\n\n{prompt}"

            # Neutral cwd: without --bare (removed — it breaks OAuth) the CLI
            # loads CLAUDE.md/AGENTS.md from its working directory. Run from
            # the system temp dir so this host repo's project context cannot
            # leak into role-contracted responses (a real run produced a
            # markdown design "consistent with existing flat modules" instead
            # of the required JSON plan because it read Genius's own CLAUDE.md).
            neutral_cwd = tempfile.gettempdir()

            process = await spawn_cli(cmd, cli_path, cwd=neutral_cwd)

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
