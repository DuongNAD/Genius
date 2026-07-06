"""NotebookLM provider - shells out to the local ``nlm`` CLI.

``nlm`` is the CLI half of the ``notebooklm-mcp-cli`` package (it ships a
``notebooklm-mcp`` MCP server too, but Genius follows its own tradition of
driving local vendor CLIs as subprocesses rather than speaking a second MCP
protocol). The CLI talks to Google NotebookLM (notebooklm.google.com) through
Google's internal API using browser cookies, so:

* Auth is shared with a one-time ``nlm login`` (or the ``NOTEBOOKLM_COOKIES``
  env var) - no API key. The ``api_key`` kwarg is accepted and ignored for
  constructor uniformity with the other providers.
* Every read command auto-emits JSON when its stdout is not a TTY (i.e. when
  we capture it), but we still pass ``--json`` explicitly so the output shape
  is deterministic regardless of the terminal.
* Errors (including auth-expired) go to **stdout**, not stderr, and exit with
  a non-zero code, so success/failure is decided by the return code and the
  message is recovered from stdout. :func:`explain_cli_failure` recognises the
  auth/quota signatures and appends an actionable hint (``nlm login``).

Unlike a general LLM, NotebookLM only answers from the *sources already in a
notebook*. So as a drop-in Researcher backend this provider queries a
**configured** notebook (a knowledge base the user has curated):
``config.models.notebooklm`` / ``GENIUS_NOTEBOOKLM_NOTEBOOK`` /
``GENIUS_MODEL_NOTEBOOKLM`` hold its id or alias. With no notebook configured
it raises (so a :class:`FallbackProvider` chain falls through to the next
backend) and points at the MCP ``notebooklm_research`` tool, which builds a
notebook from a live web search first. The module-level ``nlm_*`` coroutines
are the shared engine behind both this provider and those MCP tools.
"""

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from ag_core.interfaces.base_provider import BaseProvider, ProviderResponse, TokenUsage
from ag_core.utils.cli_resolver import memoize_cli_path, which_external
from ag_core.utils.cli_runner import (
    DEFAULT_AUX_TIMEOUT,
    communicate_with_timeout,
    explain_cli_failure,
    spawn_cli,
    tail_text,
)

logger = logging.getLogger("ag_core")

# NotebookLM's own per-query wait (its `--timeout`, default 120s). The hard
# kill sits this much higher so `nlm` can return its own timeout/answer before
# communicate_with_timeout terminates the process tree.
_DEFAULT_QUERY_TIMEOUT = 120.0
_KILL_MARGIN = 30.0
# A `research start --wait-and-import` in deep mode legitimately runs ~5 min;
# give the whole discover->import step a generous ceiling.
_DEFAULT_RESEARCH_TIMEOUT = 900.0
# Cap the question length fed to `nlm notebook query`: the question is a
# positional argv, and Windows CreateProcess rejects a command line above
# ~32767 UTF-16 units with WinError 206. Stay well under it (the workspace
# context an agent composes can be far larger, but a NotebookLM answer comes
# from the notebook's sources, not from a giant prompt).
_DEFAULT_MAX_QUERY_CHARS = 28000


def _env_float(name: str, default: float) -> float:
    """Read a positive float env var, falling back on blank/junk/non-positive."""
    raw = (os.environ.get(name) or "").strip()
    if raw:
        try:
            val = float(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return default


def _env_int(name: str, default: int) -> int:
    raw = (os.environ.get(name) or "").strip()
    if raw:
        try:
            val = int(raw)
            if val > 0:
                return val
        except ValueError:
            pass
    return default


def _query_timeout(explicit: Optional[float] = None) -> float:
    if explicit:
        try:
            val = float(explicit)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    return _env_float("GENIUS_NLM_QUERY_TIMEOUT", _DEFAULT_QUERY_TIMEOUT)


def _research_timeout(explicit: Optional[float] = None) -> float:
    if explicit:
        try:
            val = float(explicit)
            if val > 0:
                return val
        except (TypeError, ValueError):
            pass
    return _env_float("GENIUS_NLM_RESEARCH_TIMEOUT", _DEFAULT_RESEARCH_TIMEOUT)


def _max_query_chars() -> int:
    return _env_int("GENIUS_NLM_MAX_QUERY_CHARS", _DEFAULT_MAX_QUERY_CHARS)


@memoize_cli_path
def resolve_notebooklm_cli() -> str:
    """Resolve the real ``nlm`` CLI path, never a bundled repo wrapper.

    Shared by the provider, the MCP tools and the ``--doctor`` preflight.
    Precedence: ``GENIUS_NLM_PATH`` (blank treated as unset) > PATH via
    ``which_external`` > raises. There is no cross-machine default install
    location (notebooklm-mcp-cli lives in its own venv), so an explicit
    ``GENIUS_NLM_PATH`` is the supported way to point Genius at it, e.g.
    ``...\\notebooklm-mcp-cli\\venv\\Scripts\\nlm.exe``. Under pytest a
    harmless literal is returned (unit tests stub the subprocess layer).
    """
    explicit = (os.environ.get("GENIUS_NLM_PATH") or "").strip()
    if explicit:
        return explicit
    cli_path = which_external("nlm") or which_external("nlm.exe")
    if cli_path:
        return cli_path
    if os.getenv("PYTEST_CURRENT_TEST"):
        return "nlm"
    raise RuntimeError(
        "CLI 'nlm' (NotebookLM) not found. Install notebooklm-mcp-cli, set "
        "GENIUS_NLM_PATH to its nlm executable (e.g. "
        "...\\notebooklm-mcp-cli\\venv\\Scripts\\nlm.exe), run `nlm login` "
        "once, then `python serve.py --doctor` to diagnose."
    )


def _parse_nlm_json(stdout_str: str):
    """Parse ``nlm --json`` output (a dict or a list), tolerating leading noise.

    ``nlm`` prints JSON with ``json.dumps(indent=2, ensure_ascii=False)`` and
    no banner, so the whole-text parse usually wins; the span fallbacks cover
    a stray warning line some builds may prepend. Returns the parsed value or
    ``None``.
    """
    text = (stdout_str or "").strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except (ValueError, RecursionError):
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        start = text.find(open_ch)
        end = text.rfind(close_ch)
        if start != -1 and end > start:
            try:
                return json.loads(text[start : end + 1])
            except (ValueError, RecursionError):
                continue
    return None


def _extract_notebook_id(data: Any) -> Optional[str]:
    """Pull a notebook id out of ``nlm notebook create --json`` output.

    Tolerates a flat ``{"id": ...}`` or a wrapped ``{"notebook": {"id": ...}}``
    so we do not hard-depend on one exact response shape.
    """
    if isinstance(data, dict):
        for key in ("id", "notebook_id", "notebookId"):
            val = data.get(key)
            if isinstance(val, str) and val.strip():
                return val.strip()
        nested = data.get("notebook")
        if isinstance(nested, dict):
            return _extract_notebook_id(nested)
    return None


async def _run_nlm(
    args: List[str],
    *,
    timeout: Optional[float] = None,
    cli_path: Optional[str] = None,
) -> str:
    """Run ``nlm <args>`` as a subprocess and return its stdout (stripped).

    Raises :class:`RuntimeError` (via :func:`explain_cli_failure`) on a
    non-zero exit - which includes the auth-expired case, whose message ``nlm``
    prints to stdout. stdin is fed ``/dev/null``: no ``nlm`` command reads it,
    and leaving it open risks a headless hang. ``RuntimeError`` (incl.
    ``CLITimeoutError``) is what a :class:`FallbackProvider` catches to try the
    next backend.
    """
    cli_path = cli_path or resolve_notebooklm_cli()
    cmd = [cli_path, *args]
    process = await spawn_cli(cmd, cli_path, stdin=asyncio.subprocess.DEVNULL)
    stdout, stderr = await communicate_with_timeout(
        process, timeout=timeout, cli_name="nlm CLI"
    )
    stdout_str = stdout.decode("utf-8", errors="replace").strip()
    stderr_str = stderr.decode("utf-8", errors="replace").strip()
    if isinstance(process.returncode, int) and process.returncode != 0:
        raise RuntimeError(
            explain_cli_failure("nlm CLI", process.returncode, stderr_str, stdout_str)
        )
    return stdout_str


async def nlm_query(
    notebook: str,
    question: str,
    *,
    source_ids: Optional[str] = None,
    conversation_id: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Ask a NotebookLM notebook a question; return the parsed JSON result.

    Result shape (from ``nlm notebook query --json``):
    ``{"answer", "conversation_id", "sources_used", "citations",
    "references"}``.
    """
    notebook = (notebook or "").strip()
    if not notebook:
        raise RuntimeError("nlm_query requires a non-empty notebook id/alias.")
    q_timeout = _query_timeout(timeout)
    args = [
        "notebook",
        "query",
        notebook,
        question,
        "--json",
        "--timeout",
        str(int(q_timeout)),
    ]
    if source_ids:
        args += ["--source-ids", str(source_ids)]
    if conversation_id:
        args += ["--conversation-id", str(conversation_id)]
    stdout_str = await _run_nlm(args, timeout=q_timeout + _KILL_MARGIN)
    data = _parse_nlm_json(stdout_str)
    if not isinstance(data, dict):
        raise RuntimeError(
            "nlm query returned no parseable JSON object "
            f"(notebook {notebook!r}): {tail_text(stdout_str)}"
        )
    return data


async def nlm_list_notebooks(
    *, timeout: Optional[float] = None
) -> List[Dict[str, Any]]:
    """List the account's notebooks as ``[{"id", "title", "source_count", ...}]``."""
    stdout_str = await _run_nlm(
        ["notebook", "list", "--json"], timeout=timeout or DEFAULT_AUX_TIMEOUT
    )
    data = _parse_nlm_json(stdout_str)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in ("notebooks", "items", "data"):
            if isinstance(data.get(key), list):
                return data[key]
    raise RuntimeError(
        f"nlm notebook list returned no parseable JSON array: {tail_text(stdout_str)}"
    )


async def nlm_research(
    query: str,
    *,
    mode: str = "fast",
    source: str = "web",
    notebook: Optional[str] = None,
    title: Optional[str] = None,
    question: Optional[str] = None,
    timeout: Optional[float] = None,
) -> Dict[str, Any]:
    """Deep-research a topic with NotebookLM and answer from the found sources.

    Orchestrates, in order (each step guarded by its own exit code):

    1. Reuse ``notebook`` if given, else ``nlm notebook create --json`` a fresh
       one (so the id is known without parsing the research output).
    2. ``nlm research start <query> -n <id> ... --force --wait-and-import`` -
       one call that discovers web/Drive sources, waits for completion, and
       imports them into the notebook.
    3. ``nlm notebook query`` the now-enriched notebook and return the answer.

    ``mode`` is ``fast`` (~30s, ~10 sources) or ``deep`` (~5min, ~40 sources,
    web only). Returns a dict with ``notebook_id``, ``answer`` and the query
    metadata.
    """
    query = (query or "").strip()
    if not query:
        raise RuntimeError("nlm_research requires a non-empty query.")
    mode = (mode or "fast").strip().lower()
    if mode not in ("fast", "deep"):
        mode = "fast"
    source = (source or "web").strip().lower()
    if source not in ("web", "drive"):
        source = "web"
    r_timeout = _research_timeout(timeout)

    notebook_id = (notebook or "").strip()
    created = False
    if not notebook_id:
        nb_title = (title or "").strip() or f"Genius research: {query[:60]}"
        create_out = await _run_nlm(
            ["notebook", "create", nb_title, "--json"], timeout=DEFAULT_AUX_TIMEOUT
        )
        notebook_id = _extract_notebook_id(_parse_nlm_json(create_out)) or ""
        if not notebook_id:
            raise RuntimeError(
                "Could not determine the new notebook id from `nlm notebook "
                f"create` output: {tail_text(create_out)}"
            )
        created = True

    await _run_nlm(
        [
            "research",
            "start",
            query,
            "-n",
            notebook_id,
            "-s",
            source,
            "-m",
            mode,
            "--force",
            "--wait-and-import",
        ],
        timeout=r_timeout,
    )

    ask = (question or query).strip() or query
    data = await nlm_query(notebook_id, ask, timeout=timeout)
    answer = (data.get("answer") or "").strip() if isinstance(data, dict) else ""
    return {
        "notebook_id": notebook_id,
        "created_notebook": created,
        "mode": mode,
        "source": source,
        "query": query,
        "question": ask,
        "answer": answer,
        "citations": data.get("citations") if isinstance(data, dict) else None,
        "references": data.get("references") if isinstance(data, dict) else None,
    }


def _configured_notebook(model_name: str) -> str:
    """The default notebook id/alias for the provider.

    ``GENIUS_NOTEBOOKLM_NOTEBOOK`` (friendly, explicit) wins; otherwise the
    backend's ``model_name`` - which is where ``config.models.notebooklm`` /
    ``GENIUS_MODEL_NOTEBOOKLM`` land through the provider factory (the
    "model" slot doubles as the notebook selector for this backend, since
    NotebookLM exposes no model choice).
    """
    return (os.environ.get("GENIUS_NOTEBOOKLM_NOTEBOOK") or "").strip() or (
        model_name or ""
    ).strip()


def _truncate_question(question: str) -> str:
    limit = _max_query_chars()
    if len(question) <= limit:
        return question
    logger.warning(
        "[notebooklm] question of %d chars exceeds GENIUS_NLM_MAX_QUERY_CHARS=%d; "
        "truncating to stay under the Windows command-line limit.",
        len(question),
        limit,
    )
    return question[:limit]


class NotebookLMProvider(BaseProvider):
    """NotebookLM provider: answers a configured notebook via the ``nlm`` CLI."""

    def __init__(
        self,
        model_name: str = "",
        api_key: str | None = None,
        base_url: str | None = None,
        **kwargs: Any,
    ) -> None:
        # model_name doubles as the default notebook id/alias (see
        # _configured_notebook). No api_key: auth is a one-time `nlm login`.
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
            notebook = (
                extra.pop("notebook", None) or ""
            ).strip() or _configured_notebook(self.model_name)
            source_ids = extra.pop("source_ids", None)

            if not notebook:
                raise RuntimeError(
                    "NotebookLM provider: no notebook configured. Set "
                    "config.models.notebooklm (or GENIUS_NOTEBOOKLM_NOTEBOOK / "
                    "GENIUS_MODEL_NOTEBOOKLM) to a NotebookLM notebook id or "
                    "alias. For web deep-research that builds a notebook first, "
                    "use the MCP 'notebooklm_research' tool instead."
                )

            question = prompt
            if sys_prompt:
                question = (
                    f"[System instructions]\n{sys_prompt}\n\n[Question]\n{prompt}"
                )
            question = _truncate_question(question)

            data = await nlm_query(notebook, question, source_ids=source_ids)
            answer = (data.get("answer") or "").strip()
            if not answer:
                raise RuntimeError(
                    f"NotebookLM returned an empty answer for notebook "
                    f"{notebook!r} (it may have no sources, or the query "
                    f"produced no response). Raw: {tail_text(json.dumps(data))}"
                )

            # NotebookLM reports no token usage.
            response = ProviderResponse(content=answer, usage=TokenUsage())
            return response.model_dump()
