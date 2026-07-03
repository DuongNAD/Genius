"""Antigravity 2.0 (Gemini) provider - shells out to the local ``agy`` CLI.

The agy CLI is the Antigravity IDE's bundled Gemini agent binary
(``%LOCALAPPDATA%\\agy\\bin\\agy.exe`` on Windows, native .exe - no cmd.exe
wrapping needed, though the resolved-path .cmd/.bat wrapping convention is
kept for test shims). Verified invocation shape (agy 1.0.15):

* ``--print`` non-interactive mode, prompt piped via **stdin** (UTF-8). The
  prompt must never go through argv: a ~40KB argv fails with WinError 206
  ("filename or extension is too long").
* ``--dangerously-skip-permissions`` is REQUIRED in script mode - without it
  agy hangs forever on an invisible permission prompt.
* ``--sandbox`` keeps terminal restrictions enabled (verified to work with
  print mode); on by default here since permissions are skipped. Opt out with
  ``GENIUS_AGY_SANDBOX=0``.
* ``--print-timeout <Go-duration>`` makes agy give up gracefully; derived
  from ``cli_timeout()`` minus a margin so it fires before our hard kill.
* Output is PLAIN TEXT on stdout (no JSON envelope, no token usage).
  Non-zero exit or empty stdout is a failure, never a silent "" success.

Auth is shared with the Antigravity IDE login - no API key is needed (the
``api_key`` kwarg is accepted and ignored for constructor uniformity).
"""

import os
from typing import Any, Dict

from ag_core.interfaces.base_provider import BaseProvider, ProviderResponse, TokenUsage
from ag_core.utils.cli_resolver import memoize_cli_path, which_external
from ag_core.utils.cli_runner import (
    cli_timeout,
    communicate_with_timeout,
    explain_cli_failure,
    spawn_cli,
    tail_text,
)

# agy waits this much less than our hard-kill timeout (but at least this
# floor) so it can exit gracefully with its own error message first.
_PRINT_TIMEOUT_MARGIN = 10
_PRINT_TIMEOUT_FLOOR = 30


@memoize_cli_path
def resolve_agy_cli() -> str:
    """Resolve the real agy CLI path, never a bundled repo wrapper.

    Shared by send_prompt and the ``--doctor`` preflight. Precedence:
    ``GENIUS_AGY_PATH`` (blank treated as unset) > PATH via ``which_external``
    > the default Antigravity install location. Raises :class:`RuntimeError`
    when nothing is found - except under pytest, where a harmless literal is
    returned (unit tests stub the subprocess layer), matching the other
    resolvers' convention.
    """
    explicit = (os.environ.get("GENIUS_AGY_PATH") or "").strip()
    if explicit:
        return explicit
    cli_path = which_external("agy")
    if not cli_path:
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            fallback = os.path.join(localappdata, "agy", "bin", "agy.exe")
            if os.path.exists(fallback):
                cli_path = fallback
    if not cli_path:
        if os.getenv("PYTEST_CURRENT_TEST"):
            # Unit tests stub the subprocess layer; keep a harmless literal so
            # they don't require a real install.
            return "agy"
        raise RuntimeError(
            "CLI 'agy' not found; install the Antigravity CLI or set "
            "GENIUS_AGY_PATH to the executable, then run "
            "`python serve.py --doctor` to diagnose."
        )
    return cli_path


def _sandbox_enabled() -> bool:
    """``--sandbox`` is on by default; GENIUS_AGY_SANDBOX=0 disables it.

    Since ``--dangerously-skip-permissions`` is mandatory in print mode, the
    sandbox is the only remaining guardrail - keep it unless explicitly
    opted out.
    """
    raw = (os.environ.get("GENIUS_AGY_SANDBOX") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _print_timeout_seconds() -> int:
    """agy's own ``--print-timeout``, derived from the hard-kill budget."""
    return max(_PRINT_TIMEOUT_FLOOR, int(cli_timeout()) - _PRINT_TIMEOUT_MARGIN)


class AgyProvider(BaseProvider):
    """
    Antigravity 2.0 provider implementation using the local agy CLI (Gemini).
    """

    def __init__(
        self,
        model_name: str = "",
        api_key: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        # No api_key needed: auth is shared with the Antigravity IDE login.
        # An empty model_name means "use the account's default model".
        super().__init__(
            model_name=model_name or "", api_key=api_key, base_url=base_url, **kwargs
        )

    async def send_prompt(
        self, prompt: str, system: str | None = None, **kwargs: Any
    ) -> Dict[str, Any]:
        async with self.semaphore:
            await self.rate_limiter.acquire()

            extra = self.extra_params.copy()
            extra.update(kwargs)
            sys_prompt = extra.pop("system", None) or system

            cli_path = resolve_agy_cli()

            # agy has no --system-prompt flag: prepend the system text into
            # the stdin payload with explicit section markers.
            if sys_prompt:
                prompt = f"[System instructions]\n{sys_prompt}\n\n[Task]\n{prompt}"

            # The prompt is ALWAYS fed via stdin: passing it as argv fails
            # with WinError 206 above ~32KB, and would go through cmd.exe
            # metacharacter parsing when the path is a .cmd shim.
            cmd = [
                cli_path,
                "--print",
                "--dangerously-skip-permissions",
                "--print-timeout",
                f"{_print_timeout_seconds()}s",
            ]
            if _sandbox_enabled():
                cmd.append("--sandbox")
            if self.model_name:
                cmd.extend(["--model", self.model_name])

            process = await spawn_cli(cmd, cli_path)

            stdout, stderr = await communicate_with_timeout(
                process, input=prompt.encode("utf-8"), cli_name="Agy CLI"
            )

            stdout_str = stdout.decode("utf-8", errors="replace").strip()
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            if isinstance(process.returncode, int) and process.returncode != 0:
                raise RuntimeError(
                    explain_cli_failure(
                        "Agy CLI", process.returncode, stderr_str, stdout_str
                    )
                )

            # Plain-text contract: exit 0 with empty stdout is still a
            # failure - never return "" as a success.
            if not stdout_str:
                raise RuntimeError(
                    "Agy CLI produced no output (stdout was empty). "
                    f"stdout tail: {tail_text(stdout_str)} | "
                    f"stderr tail: {tail_text(stderr_str)}"
                )

            # agy reports no token usage in print mode.
            response = ProviderResponse(content=stdout_str, usage=TokenUsage())
            return response.model_dump()
