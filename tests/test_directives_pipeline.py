"""Phase 5 — PromptDirectives propagation through the pipeline.

Surgical coverage of the pieces that Phase 5 adds: _resolve_pipeline_setup's
directive parse (cleaned prompt + effort, byte-identity on the no-@ path), the
routing-strip that keeps `@deep /code ...` routing to the single /code stage,
the per-task effort contextvar (concurrency-isolated, never env), and the
skill-server RunRequest.effort field. End-to-end effort-on-the-wire is covered
by the distributed/e2e suites (no-effort byte-identity) plus the agent-level
integration tests.
"""

import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import orchestrator  # noqa: E402
from orchestrator import _resolve_pipeline_setup, PipelineError  # noqa: E402
from ag_core.skill_app import RunRequest  # noqa: E402


# --- _resolve_pipeline_setup: directive parse + effort ----------------------


def test_setup_returns_cleaned_and_effort():
    name, ws, rounds, cleaned, effort = _resolve_pipeline_setup(
        "@deep /code fix the bug", None, 0
    )
    assert cleaned == "/code fix the bug"  # @deep stripped, /cmd preserved
    assert effort == "high"
    assert name == "code_fix_the_bug"  # slug derived from the cleaned prompt


def test_setup_no_directive_is_byte_identical():
    prompt = "/code fix the bug"
    name, ws, rounds, cleaned, effort = _resolve_pipeline_setup(prompt, None, 0)
    assert cleaned is prompt  # same object -> pipeline behaviour unchanged
    assert effort is None


def test_setup_directive_only_prompt_raises():
    with pytest.raises(PipelineError):
        _resolve_pipeline_setup("@deep", None, 0)


def test_setup_plain_prompt_unchanged():
    prompt = "build a TODO API"
    _, _, _, cleaned, effort = _resolve_pipeline_setup(prompt, None, 0)
    assert cleaned is prompt
    assert effort is None


# --- adaptive effort (GENIUS_ADAPTIVE_EFFORT) --------------------------------


def test_adaptive_effort_off_by_default(monkeypatch):
    monkeypatch.delenv("GENIUS_ADAPTIVE_EFFORT", raising=False)
    _, _, _, _, effort = _resolve_pipeline_setup("build a tiny tool", None, 0)
    assert effort is None


def test_adaptive_effort_small_prompt_gets_high(monkeypatch):
    monkeypatch.setenv("GENIUS_ADAPTIVE_EFFORT", "1")
    _, _, _, _, effort = _resolve_pipeline_setup("build a tiny tool", None, 0)
    assert effort == "high"


def test_adaptive_effort_large_prompt_stays_none(monkeypatch):
    """At/over the threshold nothing changes: requests stay byte-identical to
    the pre-adaptive behaviour."""
    monkeypatch.setenv("GENIUS_ADAPTIVE_EFFORT", "1")
    big = "build a service with many requirements " * 30  # >> 600 chars
    _, _, _, _, effort = _resolve_pipeline_setup(big, None, 0)
    assert effort is None


def test_adaptive_effort_explicit_deep_wins(monkeypatch):
    """@deep is an explicit user request; the heuristic never overrides it."""
    monkeypatch.setenv("GENIUS_ADAPTIVE_EFFORT", "1")
    monkeypatch.setenv("GENIUS_ADAPTIVE_EFFORT_SMALL", "low")
    _, _, _, _, effort = _resolve_pipeline_setup("@deep build a tiny tool", None, 0)
    assert effort == "high"  # DEEP_EFFORT, not the adaptive small value


def test_adaptive_effort_threshold_and_level_tunable(monkeypatch):
    monkeypatch.setenv("GENIUS_ADAPTIVE_EFFORT", "1")
    monkeypatch.setenv("GENIUS_ADAPTIVE_EFFORT_THRESHOLD", "10")
    monkeypatch.setenv("GENIUS_ADAPTIVE_EFFORT_SMALL", "medium")
    _, _, _, _, effort = _resolve_pipeline_setup("tiny", None, 0)
    assert effort == "medium"
    _, _, _, _, effort = _resolve_pipeline_setup("longer than ten chars", None, 0)
    assert effort is None


def test_adaptive_effort_junk_level_falls_back_to_high(monkeypatch):
    monkeypatch.setenv("GENIUS_ADAPTIVE_EFFORT", "1")
    monkeypatch.setenv("GENIUS_ADAPTIVE_EFFORT_SMALL", "turbo")
    _, _, _, _, effort = _resolve_pipeline_setup("build a tiny tool", None, 0)
    assert effort == "high"


def test_adaptive_effort_junk_threshold_falls_back(monkeypatch):
    monkeypatch.setenv("GENIUS_ADAPTIVE_EFFORT", "1")
    monkeypatch.setenv("GENIUS_ADAPTIVE_EFFORT_THRESHOLD", "not-a-number")
    _, _, _, _, effort = _resolve_pipeline_setup("build a tiny tool", None, 0)
    assert effort == "high"  # default 600-char threshold applies


# --- routing strip: @modifier before /cmd still routes ----------------------


def test_routing_first_word_sees_cmd_after_strip():
    # This is the exact detection run_pipeline does; the cleaned prompt must
    # lead with /code so is_slash_cmd fires (single-command route), not run the
    # full pipeline.
    _, _, _, cleaned, _ = _resolve_pipeline_setup("@deep /code do X", None, 0)
    first_word = cleaned.strip().split()[0]
    assert first_word == "/code"
    assert first_word in orchestrator.ROUTING_TABLE


def test_routing_unchanged_for_plain_slash():
    _, _, _, cleaned, _ = _resolve_pipeline_setup("/research a topic", None, 0)
    assert cleaned.strip().split()[0] == "/research"


# --- per-task effort contextvar --------------------------------------------


def test_pipeline_effort_default_none():
    # A fresh context (direct-call/test path) has no effort set.
    async def check():
        return orchestrator._pipeline_effort()

    assert asyncio.run(check()) is None


def test_pipeline_effort_set_and_get():
    async def check():
        orchestrator._PIPELINE_EFFORT_VAR.set("high")
        return orchestrator._pipeline_effort()

    assert asyncio.run(check()) == "high"


def test_pipeline_effort_isolated_across_concurrent_tasks():
    async def worker(val):
        orchestrator._PIPELINE_EFFORT_VAR.set(val)
        await asyncio.sleep(0.01)
        # After awaiting (interleaving with the other task), still our own value.
        return orchestrator._pipeline_effort()

    async def run():
        return await asyncio.gather(worker("high"), worker(None), worker("max"))

    high, none, mx = asyncio.run(run())
    assert high == "high"
    assert none is None
    assert mx == "max"


# --- skill-server RunRequest.effort -----------------------------------------


def test_run_request_effort_defaults_none():
    assert RunRequest(prompt="hi").effort is None


def test_run_request_effort_parsed():
    assert RunRequest(prompt="hi", effort="high").effort == "high"
