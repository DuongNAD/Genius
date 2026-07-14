"""Deterministic AI-collaboration-log exporter.

Builds a submission-ready markdown log (``ai_collaboration_log.md``) for a
pipeline workspace from evidence the run already captured: the ``job.json``
manifest journaled by the MCP server, the raw per-stage agent traces under
``.genius/<slug>/logs/raw/`` (written by ``orchestrator.save_raw_response``),
and the quality sections of ``review.md``.

Design constraints (see tests/test_collab_log.py):
- stdlib only; never imports orchestrator/eval/provider modules;
- no LLM calls, no env gates: :func:`export_collab_log` is a pure function of
  the workspace tree (timestamps come from trace-file mtimes) and never
  raises on missing inputs — absent pieces degrade to explicit placeholders;
- the ONLY pipeline coupling is the opt-in auto-run at the end of the custom
  flow (``GENIUS_HACKATHON_MODE``); the module itself is always importable
  and usable standalone: ``python -m ag_core.collab_log <workspace> [--out
  PATH]``.
"""

import json
import os
import re
import sys
import time
from typing import Dict, List, Optional, Tuple

# Per-file self-heal traces: <role>_<flat path>_attempt<N>.md. The greedy
# middle group keeps flattened subjects containing underscores intact
# (codex_src_pkg_util_attempt3 -> subject "src_pkg_util", attempt 3).
_TRACE_NAME_RE = re.compile(
    r"^(?P<role>codex|tester|security)_(?P<subject>.+)_attempt(?P<attempt>\d+)$"
)
_DESIGN_RETRY_RE = re.compile(r"^design_retry(?P<attempt>\d+)$")
_ROLE_STAGE = {
    "codex": "code",
    "tester": "test-generation",
    "security": "security-audit",
}
_KNOWN_SINGLETONS = {
    "final_review": "final review",
    "final_review_fix_plan": "final review — Claude fix plan",
    "design_lint_retry1": "design (lint retry)",
    "pitch": "pitch generation",
}

_NOT_RECORDED = "(not recorded — run was not journaled)"
_USAGE = "usage: python -m ag_core.collab_log <workspace> [--out PATH]"


def _fmt_ts(unix: Optional[float]) -> str:
    """``YYYY-MM-DD HH:MM:SS`` in UTC; empty string for ``None``/bad input."""
    if unix is None:
        return ""
    try:
        return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(float(unix)))
    except (OverflowError, OSError, ValueError):
        return ""


def _one_line(text: str, limit: int = 300) -> str:
    """Collapse whitespace/newlines and truncate for a table cell."""
    flat = " ".join(str(text).split())
    if len(flat) > limit:
        flat = flat[: limit - 1] + "…"
    return flat.replace("|", "\\|")


def _load_manifest(workspace: str) -> Optional[dict]:
    """The MCP ``job.json`` manifest, or ``None`` on any error."""
    path = os.path.join(workspace, "job.json")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _trace_dirs(workspace: str) -> List[str]:
    """Raw-trace directories, newest layout first.

    Mirrors ``ag_core/eval/grader.py:_trace_dirs`` on purpose (duplicated
    rather than imported so this module stays stdlib-only and free of the
    eval package): ``<workspace>/.genius/<slug>/logs/raw/`` per slug, then
    the legacy ``<workspace>/logs/raw/``.
    """
    dirs: List[str] = []
    internal_root = os.path.join(workspace, ".genius")
    if os.path.isdir(internal_root):
        try:
            slugs = sorted(os.listdir(internal_root))
        except OSError:
            slugs = []
        for slug in slugs:
            cand = os.path.join(internal_root, slug, "logs", "raw")
            if os.path.isdir(cand):
                dirs.append(cand)
    legacy = os.path.join(workspace, "logs", "raw")
    if os.path.isdir(legacy):
        dirs.append(legacy)
    return dirs


def _classify(stem: str) -> Tuple[str, str, str]:
    """Map a trace-file stem to ``(stage, subject, attempt)`` labels."""
    m = _TRACE_NAME_RE.match(stem)
    if m is not None:
        role = m.group("role")
        return _ROLE_STAGE[role], m.group("subject"), m.group("attempt")
    m = _DESIGN_RETRY_RE.match(stem)
    if m is not None:
        return "design (format retry)", "—", m.group("attempt")
    if stem in _KNOWN_SINGLETONS:
        return _KNOWN_SINGLETONS[stem], "—", "—"
    # Unknown stems (e2e traces, future stages) pass through untranslated.
    return stem, "—", "—"


def _trace_rows(workspace: str) -> List[Dict[str, object]]:
    """One row per raw trace, sorted by ``(mtime, name)`` for a stable
    chronology."""
    rows: List[Dict[str, object]] = []
    for raw_dir in _trace_dirs(workspace):
        try:
            names = sorted(os.listdir(raw_dir))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".md"):
                continue
            path = os.path.join(raw_dir, name)
            try:
                st = os.stat(path)
            except OSError:
                continue
            stage, subject, attempt = _classify(name[: -len(".md")])
            rows.append(
                {
                    "mtime": st.st_mtime,
                    "name": name,
                    "stage": stage,
                    "subject": subject,
                    "attempt": attempt,
                    "size": st.st_size,
                }
            )
    rows.sort(key=lambda r: (r["mtime"], r["name"]))
    return rows


def _review_section(review_text: str, heading: str) -> Optional[str]:
    """Body of ``## <heading>`` up to the next ``## `` heading or EOF.

    ``###`` sub-headings do NOT terminate the section (the ``[ \\t]``
    after ``##`` cannot match the third ``#``), so nested content is kept.
    """
    pattern = re.compile(
        r"^##[ \t]+" + re.escape(heading) + r"[ \t]*\n(.*?)(?=^##[ \t]|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    m = pattern.search(review_text)
    if m is None:
        return None
    return m.group(1).strip("\n")


def export_collab_log(workspace: str) -> str:
    """Render the AI collaboration log for ``workspace`` as markdown.

    Pure and deterministic: the same tree (same contents and mtimes) always
    produces the identical string. Never raises on missing inputs.
    """
    manifest = _load_manifest(workspace) or {}
    rows = _trace_rows(workspace)

    def field(key: str) -> str:
        value = manifest.get(key)
        if value is None or value == "":
            return _NOT_RECORDED if key == "job_id" else "(not recorded)"
        return _one_line(value)

    started = _fmt_ts(manifest.get("started_at"))
    finished = _fmt_ts(manifest.get("finished_at"))

    lines: List[str] = [
        "# AI Collaboration Log",
        "",
        "All artifacts and source files in this workspace were produced by "
        "the Genius multi-agent pipeline; this log is generated "
        "deterministically from the run's own manifest and raw agent traces "
        "(no LLM involved).",
        "",
        "## Job",
        "| Field | Value |",
        "| --- | --- |",
        f"| Job ID | {field('job_id')} |",
        f"| Status | {field('status')} |",
        f"| Pipeline | {field('pipeline')} |",
        f"| Prompt | {field('prompt')} |",
        f"| Started (UTC) | {started or '(not recorded)'} |",
        f"| Finished (UTC) | {finished or '(not recorded)'} |",
        "",
        "## Stage timeline (from raw agent traces)",
    ]

    if rows:
        lines.extend(
            [
                "Timestamps are trace-file modification times (UTC); an "
                "attempt above 1 is a self-heal retry of the same stage.",
                "",
                "| # | Timestamp (UTC) | Stage | Subject | Attempt | "
                "Trace file | Size (bytes) |",
                "| --- | --- | --- | --- | --- | --- | --- |",
            ]
        )
        for idx, row in enumerate(rows, start=1):
            lines.append(
                f"| {idx} | {_fmt_ts(row['mtime'])} | {row['stage']} | "
                f"{row['subject']} | {row['attempt']} | {row['name']} | "
                f"{row['size']} |"
            )
    else:
        lines.append("(no raw traces found under .genius/<slug>/logs/raw/)")

    review_path = os.path.join(workspace, "review.md")
    try:
        with open(review_path, "r", encoding="utf-8") as fh:
            review_text = fh.read()
    except OSError:
        review_text = None

    lines.extend(["", "## Verification (copied from review.md)"])
    for heading in ("Verification coverage", "File quality states"):
        lines.append(f"### {heading}")
        if review_text is None:
            lines.append("(review.md not found)")
        else:
            body = _review_section(review_text, heading)
            lines.append(
                body if body is not None else "(review.md has no such section)"
            )
        lines.append("")

    # Provenance window: manifest timestamps, else the trace mtime span.
    start_label, end_label = started, finished
    if not (start_label and end_label) and rows:
        start_label = start_label or _fmt_ts(rows[0]["mtime"])
        end_label = end_label or _fmt_ts(rows[-1]["mtime"])
    if start_label and end_label:
        window = f"between {start_label} and {end_label} (UTC)"
    else:
        window = "during an unrecorded window"

    lines.extend(
        [
            "## Provenance statement",
            "All code and documents in this workspace were generated by AI "
            f"agents orchestrated by the Genius pipeline {window}; human "
            "input consisted of the prompt recorded above plus any "
            "stage-gate approvals. Raw per-stage agent responses are "
            "preserved under `.genius/<slug>/logs/raw/` for audit.",
            "",
        ]
    )
    return "\n".join(lines)


def refresh_log_if_present(workspace: str) -> bool:
    """Re-export ``ai_collaboration_log.md`` IN PLACE if the run emitted it.

    The pipeline exports the log BEFORE the driving process finalizes the
    ``job.json`` manifest (terminal status + ``finished_at``), so the shipped
    log would say "running" forever. Callers that finalize the manifest (the
    orchestrator CLI journal, the MCP job-completion path) call this right
    after their last manifest write; the export is deterministic, so the
    rewrite is safe. Returns ``True`` when a log existed and was rewritten,
    ``False`` when there was nothing to refresh (never creates the file).
    May raise ``OSError`` — callers treat the refresh as best-effort.
    """
    path = os.path.join(workspace, "ai_collaboration_log.md")
    if not os.path.isfile(path):
        return False
    text = export_collab_log(workspace)
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    return True


def main(argv: Optional[List[str]] = None) -> int:
    """CLI entry point. Returns 0 on success, 2 on usage errors."""
    args = list(sys.argv[1:] if argv is None else argv)
    out_path: Optional[str] = None
    if "--out" in args:
        i = args.index("--out")
        if i + 1 >= len(args):
            print(_USAGE, file=sys.stderr)
            return 2
        out_path = args[i + 1]
        del args[i : i + 2]
    if len(args) != 1:
        print(_USAGE, file=sys.stderr)
        return 2
    text = export_collab_log(args[0])
    if out_path is not None:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
