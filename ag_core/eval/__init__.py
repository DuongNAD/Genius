"""The Genius eval flywheel (R5).

A grade layer over the traces the pipeline already captures
(``orchestrator.save_raw_response`` -> ``.genius/<slug>/logs/raw/`` under the workspace).
Mirrors the shape of google/agents-cli's eval loop
(dataset -> generate -> grade -> compare) but stays local-first and
provider-agnostic: no Google Cloud / ADK dependency.

Public surface:

* :mod:`ag_core.eval.metrics` - metric definitions (deterministic
  ``CodeExecutionMetric`` + LLM-as-judge ``LLMMetric``) and the
  ``BUILTIN_METRICS`` registry.
* :mod:`ag_core.eval.grader` - ``collect_case`` (read a workspace's
  artifacts/traces) and ``grade_case`` / ``grade`` (score them).
* :mod:`ag_core.eval.judge` - the default provider-backed judge and the
  ``parse_verdict`` helper.
* :mod:`ag_core.eval.compare` - before/after regression diffing.
"""

from ag_core.eval.compare import compare
from ag_core.eval.grader import collect_case, grade, grade_case
from ag_core.eval.metrics import (
    BUILTIN_METRICS,
    DEFAULT_METRICS,
    CodeExecutionMetric,
    LLMMetric,
)

__all__ = [
    "BUILTIN_METRICS",
    "DEFAULT_METRICS",
    "CodeExecutionMetric",
    "LLMMetric",
    "collect_case",
    "grade",
    "grade_case",
    "compare",
]
