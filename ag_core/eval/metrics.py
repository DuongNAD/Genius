"""Scoring metrics for the eval flywheel (R5).

Two metric kinds, mirroring google/agents-cli:

* :class:`CodeExecutionMetric` - a deterministic Python check over the
  collected trace/artifacts. Safe and OFFLINE: it only inspects text
  (``ast.parse``, JSON parsing, file presence). It never executes the
  generated code, so it is not the remote-code-execution surface the
  stateless-bundle guardrails (see ``agent_factory.STATELESS_KWARGS``)
  are protecting against.
* :class:`LLMMetric` - an LLM-as-judge scorer. Its ``prompt_template`` is
  filled from the case (via safe placeholder substitution, NOT
  ``str.format`` - the rubric text itself contains literal ``{`` / ``}``)
  and handed to an async ``judge`` callable; the reply is parsed into a
  ``{score, explanation}`` verdict. Rubrics are adaptive: the template
  asks the judge to reason from the actual trace rather than matching a
  fixed sequence.

Scores are on a 1-5 scale, with ``0`` reserved for "not applicable /
empty input" (excluded from the overall mean). The scale matches the
agents-cli grade output so before/after numbers stay comparable.
"""

import ast
import json
import re
from typing import Awaitable, Callable, Dict, Tuple

# The judge is any async callable taking the rendered prompt and returning
# the raw model reply text (see ag_core.eval.judge.default_judge).
Judge = Callable[[str], Awaitable[str]]

# Placeholder fields an LLMMetric template may reference. Kept small and
# explicit so a template typo is a no-op rather than a KeyError.
_TEMPLATE_FIELDS = ("prompt", "research", "design", "review", "code", "trace")

# Per-field clip so a huge artifact cannot blow past the CLI argv / token
# ceilings when rendered into a judge prompt.
_CLIP_CHARS = 6000


def _clip(text: str, limit: int = _CLIP_CHARS) -> str:
    text = text or ""
    if len(text) <= limit:
        return text
    return text[:limit] + "\n...[truncated]..."


def _fill(template: str, fields: Dict[str, str]) -> str:
    """Substitute ``{field}`` placeholders without ``str.format``.

    Rubric templates embed literal JSON braces (e.g. the required
    ``{"score": ...}`` reply shape), so ``str.format`` would raise. Only
    the known :data:`_TEMPLATE_FIELDS` are replaced; everything else is
    left untouched.
    """
    out = template
    for key in _TEMPLATE_FIELDS:
        out = out.replace("{" + key + "}", _clip(str(fields.get(key, ""))))
    return out


def _scale(fraction: float) -> float:
    """Map a 0..1 fraction onto the 1..5 score scale."""
    fraction = max(0.0, min(1.0, fraction))
    return round(1.0 + 4.0 * fraction, 2)


class Metric:
    """Base metric: a name, a kind ("code"|"llm") and a description."""

    kind = "code"

    def __init__(self, name: str, description: str = "") -> None:
        self.name = name
        self.description = description


class CodeExecutionMetric(Metric):
    """A deterministic scorer: ``fn(case) -> (score, explanation)``."""

    kind = "code"

    def __init__(
        self,
        name: str,
        fn: Callable[[dict], Tuple[float, str]],
        description: str = "",
    ) -> None:
        super().__init__(name, description)
        self._fn = fn

    def evaluate(self, case: dict) -> dict:
        score, explanation = self._fn(case)
        return {
            "name": self.name,
            "kind": self.kind,
            "score": round(float(score), 2),
            "explanation": explanation,
        }


class LLMMetric(Metric):
    """An LLM-as-judge scorer driven by an adaptive rubric template."""

    kind = "llm"

    def __init__(self, name: str, prompt_template: str, description: str = "") -> None:
        super().__init__(name, description)
        self.prompt_template = prompt_template

    def render(self, case: dict) -> str:
        return _fill(self.prompt_template, case)

    async def evaluate(self, case: dict, judge: Judge) -> dict:
        raw = await judge(self.render(case))
        score, explanation = parse_verdict(raw)
        return {
            "name": self.name,
            "kind": self.kind,
            "score": score,
            "explanation": explanation,
        }


def parse_verdict(raw: str) -> Tuple[float, str]:
    """Extract a ``(score, explanation)`` from a judge's raw reply.

    Tolerant of fenced/prose-wrapped JSON: tries the first ``{...}`` block,
    then falls back to a bare ``"score": N`` regex. Score is clamped to
    ``[0, 5]``. Unparseable output scores ``0`` with the raw text kept as
    the explanation, so a flaky judge degrades to "not applicable" rather
    than a crash.
    """
    raw = (raw or "").strip()
    if not raw:
        return 0.0, "empty judge output"

    start, end = raw.find("{"), raw.rfind("}")
    if start != -1 and end > start:
        try:
            obj = json.loads(raw[start : end + 1])
            if isinstance(obj, dict) and "score" in obj:
                score = _clamp_score(obj.get("score"))
                explanation = str(obj.get("explanation", "")).strip()
                return score, explanation or "(no explanation)"
        except (ValueError, TypeError):
            pass

    m = re.search(r'"?score"?\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)', raw, re.I)
    if m:
        return _clamp_score(m.group(1)), raw[:400]

    return 0.0, f"unparseable judge output: {raw[:200]}"


def _clamp_score(value) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    return round(max(0.0, min(5.0, score)), 2)


# --------------------------------------------------------------------------
# Built-in deterministic (code) metrics
# --------------------------------------------------------------------------

_EXPECTED_ARTIFACTS = ("research", "design", "review")


def _m_artifacts_present(case: dict) -> Tuple[float, str]:
    present = [name for name in _EXPECTED_ARTIFACTS if (case.get(name) or "").strip()]
    missing = [name for name in _EXPECTED_ARTIFACTS if name not in present]
    frac = len(present) / len(_EXPECTED_ARTIFACTS)
    detail = f"present={present or 'none'}, missing={missing or 'none'}"
    return _scale(frac), detail


def _m_code_syntax_valid(case: dict) -> Tuple[float, str]:
    files = case.get("code_files") or {}
    py = {p: c for p, c in files.items() if p.endswith(".py")}
    if not py:
        return 0.0, "no Python files to check"
    ok, bad = 0, []
    for path, content in py.items():
        try:
            ast.parse(content or "")
            ok += 1
        except SyntaxError as e:
            bad.append(f"{path}: {e.msg}")
    frac = ok / len(py)
    detail = f"{ok}/{len(py)} files parse cleanly"
    if bad:
        detail += "; invalid: " + "; ".join(bad[:5])
    return _scale(frac), detail


def _m_design_wellformed(case: dict) -> Tuple[float, str]:
    """Score design.md with the SAME machinery the pipeline itself uses.

    A private single-fence extractor here used to diverge from the
    orchestrator's parser in both directions (a pipeline-accepted design
    scored "malformed" when an example JSON block preceded the plan; a
    pydantic-rejected files-as-strings plan scored 5.0). Reusing
    ``_iter_json_objects`` + the ``DesignPlan`` model keeps the metric's
    notion of "well-formed" identical to what the pipeline accepts.
    """
    text = (case.get("design") or "").strip()
    if not text:
        return 0.0, "no design artifact"
    from ag_core.models import DesignPlan
    from ag_core.orchestration_helpers import _iter_json_objects

    fenced = re.findall(r"```(?:json)?\s*(.*?)```", text, re.DOTALL | re.IGNORECASE)
    saw_candidate = False
    last_error = "no JSON object with a files[] key found"
    for chunk in fenced + [text]:
        for obj in _iter_json_objects(chunk):
            if not isinstance(obj, dict) or "files" not in obj:
                continue
            saw_candidate = True
            try:
                plan = DesignPlan(**obj)
            except Exception as e:  # pydantic ValidationError et al.
                last_error = str(e).splitlines()[0]
                continue
            if plan.files:
                return 5.0, f"well-formed DesignPlan with {len(plan.files)} file(s)"
            last_error = "files[] is empty"
    if saw_candidate:
        return 3.0, f"design JSON found but not a valid DesignPlan: {last_error}"
    return 1.0, "design contains no parseable DesignPlan JSON"


# --------------------------------------------------------------------------
# Built-in LLM-as-judge metrics (adaptive rubrics)
# --------------------------------------------------------------------------

_JSON_REPLY = (
    'Respond with ONLY a JSON object: {"score": <1-5 integer>, '
    '"explanation": "<one or two sentences>"}.'
)

_TASK_SUCCESS_TMPL = (
    "You are grading whether an autonomous coding pipeline satisfied the "
    "user's request.\n\n"
    "Original request:\n{prompt}\n\n"
    "Final design:\n{design}\n\n"
    "Generated code:\n{code}\n\n"
    "Reviewer/verification notes:\n{review}\n\n"
    "Score how completely the delivered work fulfills the original request "
    "(5 = fully satisfies every stated requirement, 1 = misses the core "
    "goal). Judge only against what was actually asked. " + _JSON_REPLY
)

_GROUNDING_TMPL = (
    "You are grading whether the design and code are grounded in the "
    "research, without invented APIs, libraries, or facts.\n\n"
    "Research:\n{research}\n\n"
    "Design:\n{design}\n\n"
    "Generated code:\n{code}\n\n"
    "Score grounding (5 = every non-trivial claim/API is supported by the "
    "research or is standard and real, 1 = relies on hallucinated or "
    "contradicted material). " + _JSON_REPLY
)

_DESIGN_QUALITY_TMPL = (
    "You are a senior software architect grading a design plan. Derive your "
    "own rubric from what THIS task actually needs (correctness, clear "
    "decomposition, error handling, testability, security), then score.\n\n"
    "Original request:\n{prompt}\n\n"
    "Design plan:\n{design}\n\n"
    "Score architectural quality (5 = a design you would approve as-is, "
    "1 = fundamentally flawed or missing). " + _JSON_REPLY
)

_FINAL_QUALITY_TMPL = (
    "You are grading the quality of the final delivered code and its "
    "review.\n\n"
    "Generated code:\n{code}\n\n"
    "Reviewer notes:\n{review}\n\n"
    "Score overall code quality: correctness, readability, error handling, "
    "and whether the review's concerns were addressed (5 = production-ready, "
    "1 = broken or unmaintainable). " + _JSON_REPLY
)


def _build_registry() -> Dict[str, Metric]:
    metrics = [
        CodeExecutionMetric(
            "artifacts_present",
            _m_artifacts_present,
            "Deterministic: which pipeline artifacts (research/design/review) "
            "were produced.",
        ),
        CodeExecutionMetric(
            "design_wellformed",
            _m_design_wellformed,
            "Deterministic: the design artifact parses as a DesignPlan JSON "
            "with project_name and files[].",
        ),
        CodeExecutionMetric(
            "code_syntax_valid",
            _m_code_syntax_valid,
            "Deterministic: fraction of generated Python files that parse "
            "without SyntaxError (never executes code).",
        ),
        LLMMetric(
            "task_success",
            _TASK_SUCCESS_TMPL,
            "LLM-judge: does the delivered work satisfy the original request?",
        ),
        LLMMetric(
            "grounding",
            _GROUNDING_TMPL,
            "LLM-judge: is the design/code grounded in research (no invented "
            "APIs/facts)?",
        ),
        LLMMetric(
            "design_quality",
            _DESIGN_QUALITY_TMPL,
            "LLM-judge: architectural quality against an adaptive rubric.",
        ),
        LLMMetric(
            "final_response_quality",
            _FINAL_QUALITY_TMPL,
            "LLM-judge: quality of the final code and whether review concerns "
            "were addressed.",
        ),
    ]
    return {m.name: m for m in metrics}


BUILTIN_METRICS: Dict[str, Metric] = _build_registry()

# Default set for ``eval grade``: only the deterministic metrics, so a grade
# runs offline (no judge, no token spend, no CLI). LLM metrics are opt-in via
# the ``metrics`` argument.
DEFAULT_METRICS = ("artifacts_present", "design_wellformed", "code_syntax_valid")
