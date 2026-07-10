import os
import asyncio
import logging
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
    wrap_windows_cmd,
    cli_timeout,
    DEFAULT_AUX_TIMEOUT,
)

logger = logging.getLogger("ag_core")

# ``grok login`` is attempted at most once per process (M1): it used to run on
# *every* send_prompt when no API key was configured (the normal local setup),
# spawning up to 5 concurrent login browsers.
_LOGIN_ATTEMPTED = False


def _skip_grok_login() -> bool:
    """Whether to skip the auto ``grok login``.

    Set ``GENIUS_GROK_SKIP_LOGIN=1`` when grok is ALREADY authenticated via the
    CLI (``grok login`` done once by hand): the auto-login re-runs ``grok login``
    with ``stdin=DEVNULL`` on the first task of every worker process, which — for
    a session/OAuth CLI — just re-prompts for login and blocks until timeout,
    even though the real call would have used the existing session fine.
    """
    return os.getenv("GENIUS_GROK_SKIP_LOGIN", "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _grok_model() -> str:
    """Model to pass to the Grok CLI via ``-m`` (headless Grok Build syntax).

    Empty by default, so no ``-m`` flag is added and the Grok CLI uses its own
    configured default model. Grok model ids are account- and CLI-version
    specific (an unknown id fails the whole call with "unknown model id"), so
    pin one explicitly via ``GENIUS_GROK_MODEL`` (e.g. ``grok-composer-2.5-fast``)
    rather than hardcoding a default that may not exist on a given install.
    """
    return os.getenv("GENIUS_GROK_MODEL", "").strip()


@memoize_cli_path
def resolve_grok_cli() -> str:
    """Resolve the real Grok CLI path, never the bundled repo wrapper.

    Shared by send_prompt and the ``--doctor`` preflight so both agree on which
    binary will actually run. Raises :class:`RuntimeError` when no real CLI is
    found - a bare-name fallback would let ``shutil.which``/``cmd.exe`` resolve
    the repo's own ``grok.cmd`` wrapper again (the recursion bug).
    """
    cli_path = which_external("grok")
    if not cli_path:
        # Official xAI Grok Build CLI installs to ~/.grok/bin (added to PATH on
        # install, but a long-running process may predate that).
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
        if os.getenv("PYTEST_CURRENT_TEST"):
            # Unit tests stub the subprocess layer; keep a harmless literal so
            # they don't require a real install.
            return "grok"
        raise RuntimeError(
            "CLI 'grok' not found; install the xAI Grok CLI or run "
            "`python serve.py --doctor` to diagnose."
        )
    return cli_path


def _wrap_windows(cmd, cli_path):
    """Compat alias for :func:`ag_core.utils.cli_runner.wrap_windows_cmd`
    (kept because tests and older callers import it from this module)."""
    return wrap_windows_cmd(cmd, cli_path)


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

    async def _maybe_login(self, cli_path: str) -> None:
        """Run ``grok login`` at most once per process when no API key is set."""
        global _LOGIN_ATTEMPTED
        if _LOGIN_ATTEMPTED:
            return
        _LOGIN_ATTEMPTED = True
        try:
            login_process = await spawn_cli(
                [cli_path, "login"], cli_path, stdin=asyncio.subprocess.DEVNULL
            )
            await communicate_with_timeout(
                login_process,
                timeout=min(DEFAULT_AUX_TIMEOUT, cli_timeout()),
                cli_name="Grok login",
            )
        except Exception as exc:
            logger.warning("Grok login attempt failed (continuing anyway): %s", exc)

    async def send_prompt(
        self, prompt: str, system: str | None = None, **kwargs: Any
    ) -> Dict[str, Any]:
        async with self.semaphore:
            await self.rate_limiter.acquire()

            extra = self.extra_params.copy()
            extra.update(kwargs)
            sys_prompt = extra.pop("system", None) or system

            cli_path = resolve_grok_cli()

            if not self.api_key and not _skip_grok_login():
                await self._maybe_login(cli_path)

            temp_file_path = None
            try:
                # Always pass the prompt via --prompt-file: inlining it as a
                # `-p <prompt>` argument goes through cmd.exe on Windows,
                # which interprets `&|<>^%` and newlines in LLM-generated
                # prompt text (garbling/injection) and hits the command-line
                # length limit.
                # Fold the system prompt into the file too. Passing it as a
                # --system-prompt-override argv element has the same Windows
                # cmd.exe problem the prompt itself avoids: on a .cmd shim the
                # text is truncated at the first newline and &|<>^% is
                # interpreted, silently dropping most of the system contract.
                file_content = prompt
                if sys_prompt:
                    file_content = (
                        f"[SYSTEM INSTRUCTIONS]\n{sys_prompt}\n\n"
                        f"[USER REQUEST]\n{prompt}"
                    )
                with tempfile.NamedTemporaryFile(
                    mode="w", suffix=".txt", delete=False, encoding="utf-8"
                ) as f:
                    f.write(file_content)
                    temp_file_path = f.name
                cmd = [
                    cli_path,
                    "--prompt-file",
                    temp_file_path,
                    "--output-format",
                    "json",
                ]

                # Pin the model with ``-m`` when GENIUS_GROK_MODEL is set (no
                # flag by default → the CLI's own default model). Appended AFTER
                # the base args so cmd[1] stays "--prompt-file" (the login-vs-
                # prompt call is told apart by cmd[1]).
                model = _grok_model()
                if model:
                    cmd.extend(["-m", model])

                session_id = (
                    extra.pop("session_id", None)
                    or kwargs.get("session_id")
                    or self.extra_params.get("session_id")
                )
                if session_id:
                    cmd.extend(["--session-id", str(session_id)])

                process = await spawn_cli(
                    cmd, cli_path, stdin=asyncio.subprocess.DEVNULL
                )

                stdout, stderr = await communicate_with_timeout(
                    process, cli_name="Grok CLI"
                )
            finally:
                if temp_file_path and os.path.exists(temp_file_path):
                    try:
                        os.remove(temp_file_path)
                    except Exception:
                        pass

            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            if isinstance(process.returncode, int) and process.returncode != 0:
                raise RuntimeError(
                    explain_cli_failure(
                        "Grok CLI", process.returncode, stderr_str, stdout_str
                    )
                )

            res_json = extract_json_object(stdout_str) or {}

            # Grok reports some failures with exit code 0 and an error-shaped
            # JSON payload, e.g. {"type":"error","message":"...403 Forbidden
            # ...spending-limit..."}. Surface it instead of returning "".
            if res_json.get("type") == "error" or res_json.get("is_error"):
                error_msg = str(res_json.get("message") or res_json.get("result") or "")
                raise RuntimeError(
                    f"Grok CLI reported an error: {error_msg} | "
                    f"stderr tail: {tail_text(stderr_str)}"
                )

            # Grok CLI builds disagree on the answer key. The older "build" CLI
            # returns {"result": ...}; the current xAI Grok CLI returns
            # {"text": ..., "stopReason": ..., "sessionId": ..., "thought": ...}
            # — where "thought" is the model's internal reasoning and MUST NOT
            # leak into the answer. Try the known answer keys in priority order.
            # (An error-shaped payload was already handled above, so "message"
            # is deliberately not treated as an answer key here.)
            content = ""
            for key in ("result", "text", "response", "output"):
                val = res_json.get(key)
                if val:
                    content = val if isinstance(val, str) else str(val)
                    break
            if not content:
                # Some Grok CLI builds/modes ignore --output-format json and just
                # print the answer as plain text (no JSON envelope at all). On a
                # clean exit (returncode 0, checked above) treat the raw stdout as
                # the answer rather than discarding a valid non-JSON response —
                # but only when it decoded CLEANLY: undecodable/binary output
                # (U+FFFD replacement chars or NUL bytes) is corruption, not an
                # answer, and must still surface as an error.
                if stdout_str and "�" not in stdout_str and "\x00" not in stdout_str:
                    content = stdout_str
            if not content:
                raise RuntimeError(
                    "Grok CLI produced no result (output was empty, undecodable, "
                    "or not the expected JSON envelope). "
                    f"stdout tail: {tail_text(stdout_str)} | "
                    f"stderr tail: {tail_text(stderr_str)}"
                )

            # `or {}` (not a get() default): a JSON `"usage": null` makes
            # get("usage", {}) return None, and None.get() would crash — throwing
            # away an already-parsed valid answer.
            usage = res_json.get("usage") or {}
            prompt_tokens = usage.get("input_tokens", 0)
            completion_tokens = usage.get("output_tokens", 0)
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
