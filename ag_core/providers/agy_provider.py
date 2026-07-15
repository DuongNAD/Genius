"""Antigravity 2.0 (Gemini) provider - shells out to the local ``agy`` CLI.

The agy CLI is the Antigravity IDE's bundled Gemini agent binary
(``%LOCALAPPDATA%\\agy\\bin\\agy.exe`` on Windows, native .exe - no cmd.exe
wrapping needed, though the resolved-path .cmd/.bat wrapping convention is
kept for test shims). Verified invocation shape (agy 1.1.1):

* ``--print`` / ``-p`` is a STRING flag whose VALUE is the prompt — agy does
  NOT read the prompt from stdin. A bare ``--print`` silently consumes the
  next token as its value, so the prompt is passed as ``--print=<prompt>``
  (the ``=`` form protects a prompt that begins with ``-``). This puts the
  prompt in argv, so a >~32KB prompt can hit the Windows CreateProcess limit;
  stdin, however, never reaches agy, so argv is the only working channel.
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
import sys
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

# The prompt travels as ONE argv string (--print=<prompt>), so it is bounded
# by the OS argument limits, not by the model's context window: Windows
# CreateProcess caps the whole command line at ~32K chars, Linux caps a single
# argv string at MAX_ARG_STRLEN (128 KiB), macOS caps argv+env at ARG_MAX
# (1 MiB total). An over-limit spawn dies as OSError(E2BIG) — an exception
# FallbackProvider does NOT catch — so the guard below converts the condition
# into a clean RuntimeError BEFORE spawning, letting the chain fall through to
# a stdin-capable backend (claude/codex). Tune with GENIUS_AGY_MAX_PROMPT_BYTES.
_ARGV_PROMPT_LIMITS = {"win32": 28_000, "linux": 120_000}
_ARGV_PROMPT_LIMIT_DEFAULT = 800_000  # darwin & others: 1 MiB ARG_MAX - headroom


def max_prompt_bytes() -> int:
    """Effective agy prompt byte cap: env override > per-platform default."""
    raw = (os.environ.get("GENIUS_AGY_MAX_PROMPT_BYTES") or "").strip()
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    for prefix, limit in _ARGV_PROMPT_LIMITS.items():
        if sys.platform.startswith(prefix):
            return limit
    return _ARGV_PROMPT_LIMIT_DEFAULT


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
        # Validate eagerly: an explicit-but-wrong path otherwise surfaces as a
        # bare "[Errno 2] No such file or directory" from the subprocess layer
        # (a live fallback drill hit exactly that) — name the culprit instead.
        # Still a RuntimeError, so a FallbackProvider chain moves on cleanly.
        # Unit tests set fake literals and stub the subprocess layer, so the
        # check is skipped under pytest (same convention as below).
        if not os.path.exists(explicit) and not os.getenv("PYTEST_CURRENT_TEST"):
            raise RuntimeError(
                f"agy CLI not found at GENIUS_AGY_PATH={explicit!r}; fix or "
                "unset the variable, then run `python serve.py --doctor`."
            )
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
        self,
        prompt: str,
        system: str | None = None,
        *,
        effort: str | None = None,  # accepted for interface parity; agy has no effort flag
        **kwargs: Any,
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

            # agy's ``--print`` / ``-p`` is a STRING flag whose VALUE is the
            # prompt — it does NOT read the prompt from stdin. A bare
            # ``--print`` consumes the NEXT token as its value (so the old
            # stdin form made agy treat ``--dangerously-skip-permissions`` as
            # the prompt and ignore the real request). Pass the prompt as the
            # flag value, using the ``--print=<value>`` form so a prompt that
            # begins with ``-`` is never mistaken for another flag. Keep it
            # LAST so the earlier bool/value flags parse cleanly.
            # Trade-off: this reintroduces the argv size limit that stdin
            # avoided (Windows CreateProcess ~32KB) — but stdin simply does not
            # reach agy, so argv is the only channel that works.
            cmd = [
                cli_path,
                "--dangerously-skip-permissions",
                "--print-timeout",
                f"{_print_timeout_seconds()}s",
            ]
            if _sandbox_enabled():
                cmd.append("--sandbox")
            if self.model_name:
                cmd.extend(["--model", self.model_name])
            cmd.append(f"--print={prompt}")

            prompt_bytes = len(prompt.encode("utf-8"))
            limit = max_prompt_bytes()
            if prompt_bytes > limit:
                raise RuntimeError(
                    f"Agy CLI prompt is {prompt_bytes} bytes, over the "
                    f"{limit}-byte argv cap (agy only accepts the prompt via "
                    "--print=<value> in argv; an oversized spawn dies with "
                    "E2BIG). Shrink the context (GENIUS_CONTEXT_TOKEN_BUDGET) "
                    "or raise GENIUS_AGY_MAX_PROMPT_BYTES; a fallback chain "
                    "moves on to a stdin-capable backend."
                )

            try:
                process = await spawn_cli(cmd, cli_path)
            except OSError as exc:
                # E2BIG & friends at exec time: surface as RuntimeError so a
                # FallbackProvider chain falls through instead of dying.
                raise RuntimeError(
                    f"Agy CLI could not be launched ({exc}); prompt was "
                    f"{prompt_bytes} bytes — if this is an argv-size failure, "
                    "lower GENIUS_AGY_MAX_PROMPT_BYTES or shrink the context."
                ) from exc

            # Prompt travels in argv (above); stdin is intentionally empty.
            stdout, stderr = await communicate_with_timeout(
                process, input=b"", cli_name="Agy CLI"
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
