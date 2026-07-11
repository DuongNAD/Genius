"""Pure, self-contained orchestrator helpers.

Extracted from ``orchestrator.py`` (which re-imports them, so ``orchestrator.<name>``
and ``from orchestrator import <name>`` keep working and stay monkeypatch-able).
These are leaf functions: CLI-command resolution, argv formatting, JSON-stream
parsing, and security-report verdict parsing. They reference no pipeline globals
(``DISTRIBUTED_MODE``) and no patched I/O (``call_api``/``run_subprocess``/
``verify_response_checksum``), so moving them cannot change dispatch or checksum
behaviour.
"""

import json
import os
import re
import shutil
import sys

from ag_core.orchestration_errors import PipelineError
from ag_core.utils.logger import logger


def _iter_json_objects(text: str):
    """Yield every top-level JSON object decodable from ``text``.

    Brace-aware scan via ``raw_decode`` so a ``}`` inside a string value
    cannot truncate the object the way a ``\\{.*?\\}`` / find..rfind regex
    would. Shared by the design-plan and security-verdict parsers.
    """
    decoder = json.JSONDecoder()
    idx = 0
    while True:
        start = text.find("{", idx)
        if start == -1:
            return
        try:
            obj, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            idx = start + 1
            continue
        yield obj
        idx = start + end


def resolve_grok_cmd():
    return "grok"


def resolve_claude_cmd():
    if sys.platform.startswith("win"):
        user_profile = os.environ.get("USERPROFILE") or os.path.expanduser("~")
        special_path = os.path.join(user_profile, ".local", "bin", "claude.exe")
        if os.path.exists(special_path):
            return special_path
        resolved = shutil.which("claude.exe") or shutil.which("claude")
        return resolved or "claude"
    else:
        resolved = shutil.which("claude")
        return resolved or "claude"


def resolve_antigravity_cmd():
    env_path = os.environ.get("ANTIGRAVITY_BIN_PATH")
    if env_path:
        return env_path

    if sys.platform.startswith("win"):
        user_profile = os.environ.get("USERPROFILE") or os.environ.get("HOME")
        special_paths = []
        if user_profile:
            special_paths.append(
                os.path.join(
                    user_profile, ".gemini", "antigravity", "bin", "antigravity.cmd"
                )
            )
            special_paths.append(
                os.path.join(
                    user_profile, ".gemini", "antigravity", "bin", "antigravity"
                )
            )
        for path in special_paths:
            if os.path.exists(path):
                return path
        resolved = shutil.which("antigravity.cmd") or shutil.which("antigravity")
        return resolved or "antigravity.cmd"
    else:
        resolved = shutil.which("antigravity")
        return resolved or "antigravity"


def resolve_codex_cmd():
    return "codex"


def resolve_tester_cmd():
    return "tester"


def resolve_security_cmd():
    return "security"


def resolve_devops_cmd():
    return "devops"


def clean_output_files(paths):
    """Archive context/output files from a previous run to ``<name>.bak``
    (overwriting an older .bak) so a fresh run cannot consume stale data but
    the previous artifacts are still recoverable."""
    logger.info("Archiving old context/output files...")
    for path in paths:
        if os.path.exists(path):
            try:
                backup_path = path + ".bak"
                os.replace(path, backup_path)
                logger.info(f"Archived old file: {path} -> {backup_path}")
            except Exception as e:
                logger.error(f"Failed to archive {path}: {e}")
                raise PipelineError(f"Failed to archive {path}: {e}")


def format_cmd_args(
    cmd_executable, args_template, prompt, input_path=None, output_path=None
):
    """Format command arguments by replacing placeholders with actual values."""
    cmd = [cmd_executable]

    input_content = ""
    if input_path and os.path.exists(input_path):
        try:
            with open(input_path, "r", encoding="utf-8") as f:
                input_content = f.read()
        except Exception as e:
            logger.error(f"Failed to read input file {input_path} for formatting: {e}")
            raise PipelineError(
                f"Failed to read input file {input_path} for formatting: {e}"
            )

    for arg in args_template:
        formatted = arg
        if "{prompt}" in formatted:
            formatted = formatted.replace("{prompt}", prompt)
        if "{input}" in formatted and input_path:
            formatted = formatted.replace("{input}", input_path)
        if "{input_content}" in formatted:
            formatted = formatted.replace("{input_content}", input_content)
        if "{output}" in formatted and output_path:
            formatted = formatted.replace("{output}", output_path)
        cmd.append(formatted)

    return cmd


def detect_vulnerabilities(security_report: str) -> bool:
    """
    Decide whether a security audit report indicates a real, actionable vulnerability.

    Replaces naive case-sensitive substring matching (which produced false positives
    on phrases like "no HIGH severity issues found" or "HIGHLY recommended") with
    word-boundary matching plus negation-aware context checks.

    Returns True only when an explicit vulnerability marker or a high/critical
    severity term is present in a non-negated context.
    """
    if not security_report:
        return False

    text = security_report.lower()

    # 1. Explicit machine-readable markers emitted intentionally by the audit.
    explicit_markers = ("[vulnerability detected]", "[insecure]", "[vulnerable]")
    if any(marker in text for marker in explicit_markers):
        return True

    # 2. Severity terms matched on word boundaries (so "highly"/"highlight" don't match).
    severity_pattern = re.compile(
        r"\b(high|critical)\b(?:\s+(?:severity|risk|vulnerabilit\w+|issue\w*))?",
        re.IGNORECASE,
    )

    # Negation cues that flip a severity hit into a "clean" statement, e.g.
    # "no high severity issues", "0 critical vulnerabilities", "without critical".
    negation_pattern = re.compile(r"\b(no|none|zero|0|without|free of|not? any)\b")

    for match in severity_pattern.finditer(text):
        # Inspect the ~40 chars preceding the match for a negation cue.
        window_start = max(0, match.start() - 40)
        preceding = text[window_start : match.start()]
        if negation_pattern.search(preceding):
            continue
        return True

    return False


def parse_security_verdict(security_report: str):
    """
    Extract a structured security verdict {"blocking": bool, "findings": [...]}
    from the audit report. Returns the dict, or None if no verdict object is present.
    """
    if not security_report:
        return None
    fenced = re.findall(
        r"```json\s*(.*?)```", security_report, re.DOTALL | re.IGNORECASE
    )
    for text in fenced + [security_report]:
        for obj in _iter_json_objects(text):
            if isinstance(obj, dict) and "blocking" in obj:
                return obj
    return None


# security_is_blocking lives in orchestrator.py (not here) so it resolves
# parse_security_verdict/detect_vulnerabilities through orchestrator's namespace,
# keeping those monkeypatch points effective.
