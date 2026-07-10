import os
import glob
import json
import asyncio
from typing import Any, Dict, List, Tuple

from ag_core.interfaces.base_provider import BaseProvider, ProviderResponse, TokenUsage
from ag_core.utils.cli_resolver import memoize_cli_path, which_external
from ag_core.utils.cli_runner import (
    communicate_with_timeout,
    explain_cli_failure,
    spawn_cli,
    tail_text,
)

# Codex is a login-based desktop CLI (ChatGPT auth). If a machine also exports
# OPENAI_API_KEY / OPENAI_BASE_URL (e.g. a LiteLLM or other OpenAI-compatible
# proxy on localhost), codex silently routes through that proxy in API-key
# mode instead of the ChatGPT login. In login mode (the default) those two
# vars are stripped from the codex subprocess ONLY, so the machine-wide proxy
# env is left intact for every other tool. Set GENIUS_CODEX_LOGIN_MODE to a
# falsy value (0/false/no/off) to inherit them (i.e. keep proxy/API-key mode).
_CODEX_LOGIN_STRIP = ("OPENAI_API_KEY", "OPENAI_BASE_URL")


def _codex_subprocess_env():
    """Environment for the codex subprocess.

    Returns ``None`` (inherit the parent env unchanged) when login mode is
    off, or when none of the proxy vars are set so there is nothing to hide.
    Otherwise returns a copy of ``os.environ`` with the proxy OPENAI_* vars
    removed, so codex falls back to its ChatGPT login.
    """
    if os.getenv("GENIUS_CODEX_LOGIN_MODE", "1").strip().lower() in (
        "0",
        "false",
        "no",
        "off",
    ):
        return None
    if not any(k in os.environ for k in _CODEX_LOGIN_STRIP):
        return None
    env = os.environ.copy()
    for k in _CODEX_LOGIN_STRIP:
        env.pop(k, None)
    return env


def _newest(paths):
    """Return the most recently modified path, tolerating ones that don't exist.

    The Codex desktop app keeps binaries in content-addressed ``bin/<hash>``
    dirs and leaves stale ones behind after an update, so several ``codex.exe``
    copies can coexist; pick the newest. ``getmtime`` is guarded so a path that
    vanished (or a mocked, non-existent path in tests) sorts last instead of
    raising.
    """

    def mtime(p):
        try:
            return os.path.getmtime(p)
        except OSError:
            return 0.0

    return max(paths, key=mtime)


@memoize_cli_path
def resolve_codex_cli() -> str:
    """Resolve the Codex CLI path (PATH, then Codex desktop install dirs).

    Shared by send_prompt and the ``--doctor`` preflight. Never returns the
    bundled repo wrapper (``which_external`` excludes it). Raises
    :class:`RuntimeError` when no real CLI is found - a bare-name fallback
    would let ``shutil.which``/``cmd.exe`` resolve the repo's own
    ``codex.cmd`` wrapper again (the recursion bug).
    """
    cli_path = which_external("codex") or which_external("codex.exe")
    if not cli_path:
        localappdata = os.environ.get("LOCALAPPDATA")
        if localappdata:
            pattern1 = os.path.join(
                localappdata, "OpenAI", "Codex", "bin", "*", "codex.exe"
            )
            matches1 = glob.glob(pattern1)
            if matches1:
                cli_path = _newest(matches1)

        if not cli_path and localappdata:
            candidate2 = os.path.join(
                localappdata, "Microsoft", "WindowsApps", "codex.exe"
            )
            if os.path.exists(candidate2):
                cli_path = candidate2

        if not cli_path:
            program_files = os.environ.get("ProgramFiles")
            if program_files:
                pattern3 = os.path.join(
                    program_files,
                    "WindowsApps",
                    "OpenAI.Codex_*",
                    "app",
                    "resources",
                    "codex.exe",
                )
                matches3 = glob.glob(pattern3)
                if matches3:
                    cli_path = _newest(matches3)

        if not cli_path:
            if os.getenv("PYTEST_CURRENT_TEST"):
                # Unit tests stub the subprocess layer; keep a harmless
                # literal so they don't require a real install.
                return "codex" if os.name != "nt" else "codex.exe"
            raise RuntimeError(
                "CLI 'codex' not found; install the OpenAI Codex CLI/desktop "
                "app or run `python serve.py --doctor` to diagnose."
            )
    return cli_path


def _parse_codex_jsonl(stdout_str: str) -> Tuple[str, List[str], int, int, int]:
    """Parse ``codex exec --json`` JSONL output.

    Returns ``(content, error_parts, prompt_tokens, completion_tokens,
    total_tokens)``. Pure and synchronous — and CPU-heavy on huge streams
    (the robustness suite feeds 50k-line outputs) — so ``send_prompt`` runs
    it in a worker thread via ``asyncio.to_thread`` to keep the event loop
    responsive for the other in-flight prompts.
    """
    content_parts = []
    error_parts = []
    prompt_tokens = 0
    completion_tokens = 0
    total_tokens = 0

    lines = stdout_str.splitlines()
    accumulator = []

    # Helper to convert values to int safely
    def safe_int(val) -> int | None:
        try:
            return int(val)
        except (ValueError, TypeError):
            return None

    for line in lines:
        try:
            line_stripped = line.strip()
            if not line_stripped:
                continue

            # Strip prefix noise if present
            idx = -1
            for char in ("{", "["):
                pos = line_stripped.find(char)
                if pos != -1 and (idx == -1 or pos < idx):
                    idx = pos
            if idx != -1:
                line_stripped = line_stripped[idx:]

            # Try to parse line directly first
            data = None
            try:
                data = json.loads(line_stripped)
                accumulator = (
                    []
                )  # Clear accumulator on successful parse of a single line
            except (Exception, RecursionError):
                # Line itself is not valid JSON, so we append to accumulator
                accumulator.append(line)
                if len(accumulator) > 50:
                    accumulator = accumulator[-50:]

                # Clean leading lines in accumulator that cannot start JSON
                while accumulator and not (
                    accumulator[0].strip().startswith("{")
                    or accumulator[0].strip().startswith("[")
                ):
                    accumulator.pop(0)

                # Try to parse from suffix starting points to recover from noise
                for i in range(len(accumulator)):
                    # Only try suffixes that start with { or [ to avoid parsing nested structures
                    suffix_start = accumulator[i].strip()
                    if not (
                        suffix_start.startswith("{") or suffix_start.startswith("[")
                    ):
                        continue

                    # Check prefix for any unmatched open braces to prevent parsing inner objects too early
                    prefix_text = "".join(accumulator[:i])
                    opens = prefix_text.count("{") + prefix_text.count("[")
                    closes = prefix_text.count("}") + prefix_text.count("]")
                    if opens > closes:
                        continue  # Likely nested inside an unmatched outer structure in the prefix

                    try:
                        accumulated_str = "\n".join(accumulator[i:])
                        parsed = json.loads(accumulated_str)
                        accumulator = []  # Clear on success
                        if isinstance(parsed, dict):
                            data = parsed
                        break
                    except (Exception, RecursionError):
                        pass

                if data is None:
                    continue

            if not isinstance(data, dict):
                continue

            event_type = data.get("event") or data.get("type")

            # Collect failure events so an empty run raises with the
            # real cause instead of returning "" as a success.
            if event_type in ("error", "turn.failed"):
                err = data.get("message")
                if err is None and isinstance(data.get("error"), dict):
                    err = data["error"].get("message")
                error_parts.append(str(err) if err else json.dumps(data))

            item = data.get("item")
            if not isinstance(item, dict):
                item = {}

            is_agent_msg = False
            if event_type == "agent_message" or item.get("type") == "agent_message":
                is_agent_msg = True
            elif "agent_message" in data:
                am = data.get("agent_message")
                if isinstance(am, dict):
                    is_agent_msg = True
                    if am.get("item") and isinstance(am.get("item"), dict):
                        item = am.get("item")
            elif event_type == "item.completed" and isinstance(data.get("item"), dict):
                item = data.get("item")
                if isinstance(item, dict) and (
                    item.get("type") == "agent_message"
                    or item.get("event") == "agent_message"
                ):
                    is_agent_msg = True

            if is_agent_msg:
                text = item.get("text") if isinstance(item, dict) else None
                if text is None:
                    text = data.get("text")
                if text is None and "agent_message" in data:
                    am = data.get("agent_message")
                    if isinstance(am, dict):
                        text = am.get("text")
                if text is not None:
                    content_parts.append(str(text))

            # Check for turn.completed event or equivalent keys
            if event_type == "turn.completed" or "turn.completed" in data:
                # Gather all possible dictionaries containing token counts
                candidate_dicts = []

                # Helper to add a dict safely
                def add_candidate(d):
                    if isinstance(d, dict) and d not in candidate_dicts:
                        candidate_dicts.append(d)

                add_candidate(data)

                turn_completed = data.get("turn.completed")
                if isinstance(turn_completed, dict):
                    add_candidate(turn_completed)
                    add_candidate(turn_completed.get("usage"))
                    add_candidate(turn_completed.get("tokens"))

                turn_val = data.get("turn")
                if isinstance(turn_val, dict):
                    completed_val = turn_val.get("completed")
                    if isinstance(completed_val, dict):
                        add_candidate(completed_val)
                        add_candidate(completed_val.get("usage"))
                        add_candidate(completed_val.get("tokens"))

                add_candidate(data.get("usage"))
                add_candidate(data.get("tokens"))

                input_val = None
                output_val = None
                total_val = None

                for d in candidate_dicts:
                    if input_val is None:
                        val = d.get("input_tokens") or d.get("prompt_tokens")
                        parsed = safe_int(val)
                        if parsed is not None:
                            input_val = parsed

                    if output_val is None:
                        val = d.get("output_tokens") or d.get("completion_tokens")
                        parsed = safe_int(val)
                        if parsed is not None:
                            output_val = parsed

                    if total_val is None:
                        val = d.get("total_tokens") or d.get("total")
                        parsed = safe_int(val)
                        if parsed is not None:
                            total_val = parsed

                if input_val is not None:
                    prompt_tokens = input_val
                if output_val is not None:
                    completion_tokens = output_val
                if total_val is not None:
                    total_tokens = total_val
        except Exception:
            continue

    if total_tokens == 0:
        total_tokens = prompt_tokens + completion_tokens

    content = "".join(content_parts)
    return content, error_parts, prompt_tokens, completion_tokens, total_tokens


class OpenAIProvider(BaseProvider):
    """
    OpenAI API provider implementation using the local codex CLI.
    """

    def __init__(
        self,
        model_name: str = "",
        api_key: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        # An empty model_name means "use the codex CLI's configured default
        # model"; a value is passed through as `-m/--model`. Never default to
        # a chat-API name like gpt-4o here — codex would reject it.
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        base_url = (
            base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        )
        super().__init__(
            model_name=model_name, api_key=api_key, base_url=base_url, **kwargs
        )

    async def send_prompt(
        self,
        prompt: str,
        system: str | None = None,
        workdir: str | None = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        async with self.semaphore:
            await self.rate_limiter.acquire()

            extra = self.extra_params.copy()
            extra.update(kwargs)
            sys_prompt = extra.pop("system", None) or system
            workdir = extra.pop("workdir", None) or workdir

            # Locate codex prioritized in PATH, then Codex desktop install dirs
            cli_path = resolve_codex_cli()

            if sys_prompt:
                prompt = f"{sys_prompt}\n\n{prompt}"

            # `codex exec [PROMPT]` treats the positional arg as the literal
            # instructions — it has no flag to read the prompt from a file.
            # Passing a temp-file PATH (the old behaviour) made Codex run with
            # the path string as its instructions. Instead use "-" so Codex
            # reads the prompt from stdin; this also sidesteps the Windows
            # command-line length limit the temp file was trying to avoid.
            #
            # Sandbox policy: Codex is an AGENTIC CLI that will happily run
            # shell commands from its cwd. With the old
            # --dangerously-bypass-approvals-and-sandbox default it once ran
            # this repo's entire test suite and returned the pytest log as its
            # "implementation". Default to a read-only sandbox so Codex can
            # think but not execute; --skip-git-repo-check keeps exec mode
            # working outside a git repo. GENIUS_CODEX_SANDBOX overrides:
            #   read-only (default) | workspace-write | danger (old bypass).
            # Legacy values 1/true/yes ("keep the sandbox on") map to the
            # read-only default; unknown values fail safe to read-only.
            sandbox_mode = os.getenv("GENIUS_CODEX_SANDBOX", "").strip().lower()
            if sandbox_mode == "danger":
                sandbox_flags = ["--dangerously-bypass-approvals-and-sandbox"]
            elif sandbox_mode == "workspace-write":
                sandbox_flags = [
                    "--sandbox",
                    "workspace-write",
                    "--skip-git-repo-check",
                ]
            else:
                sandbox_flags = ["--sandbox", "read-only", "--skip-git-repo-check"]

            cmd = [cli_path, "exec", "-", *sandbox_flags]
            if self.model_name:
                cmd += ["-m", self.model_name]
            # Opt-in reasoning-effort override (default off). Genius-scoped:
            # `-c model_reasoning_effort=<v>` overrides ~/.codex/config.toml for
            # THIS call only, so a globally-configured "ultra" can be dialed to
            # a cheaper "high" for the pipeline without editing the user's codex
            # config. Passed through verbatim (codex validates the value).
            codex_effort = os.getenv("GENIUS_CODEX_EFFORT", "").strip().lower()
            if codex_effort:
                cmd += ["-c", f"model_reasoning_effort={codex_effort}"]
            if workdir:
                # Point Codex's working root at a caller-provided directory so
                # even a writable sandbox can only touch that tree.
                cmd += ["--cd", str(workdir)]
            cmd.append("--json")

            prompt_bytes = prompt.encode("utf-8")
            process = await spawn_cli(cmd, cli_path, env=_codex_subprocess_env())
            stdout, stderr = await communicate_with_timeout(
                process, input=prompt_bytes, cli_name="Codex CLI"
            )

            stdout_str = stdout.decode("utf-8", errors="replace")
            stderr_str = stderr.decode("utf-8", errors="replace").strip()

            if isinstance(process.returncode, int) and process.returncode != 0:
                raise RuntimeError(
                    explain_cli_failure(
                        "Codex CLI", process.returncode, stderr_str, stdout_str
                    )
                )

            (
                content,
                error_parts,
                prompt_tokens,
                completion_tokens,
                total_tokens,
            ) = await asyncio.to_thread(_parse_codex_jsonl, stdout_str)
            if not content:
                detail = (
                    "; ".join(error_parts)
                    if error_parts
                    else "no agent_message content in output"
                )
                raise RuntimeError(
                    f"Codex CLI produced no content: {detail} | "
                    f"stdout tail: {tail_text(stdout_str)} | "
                    f"stderr tail: {tail_text(stderr_str)}"
                )

            response = ProviderResponse(
                content=content,
                usage=TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                ),
            )
            return response.model_dump()
