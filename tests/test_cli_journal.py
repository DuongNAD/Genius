"""CLI job.json journaling (orchestrator.run_cli_journaled).

The CLI counterpart of the MCP server's _journal_job: a plain
``python orchestrator.py`` run must journal the same manifest shape into its
workspace (running -> completed/failed) and, after the final write, refresh
an already-emitted ai_collaboration_log.md so the shipped log carries the
terminal status instead of "running".
"""

import asyncio
import json
import os
import re
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from orchestrator import PipelineError, run_cli_journaled  # noqa: E402
from ag_core.collab_log import export_collab_log  # noqa: E402


def _read_manifest(ws):
    with open(os.path.join(ws, "job.json"), encoding="utf-8") as fh:
        return json.load(fh)


def test_manifest_written_on_success(tmp_path):
    async def fake_pipeline():
        # The manifest must already exist (status running) DURING the run —
        # that is what _emit_hackathon_artifacts' export sees.
        mid = _read_manifest(str(tmp_path))
        assert mid["status"] == "running"
        assert mid["finished_at"] is None
        return "pipeline-result"

    result = asyncio.run(
        run_cli_journaled(
            fake_pipeline,
            workspace=str(tmp_path),
            pipeline="custom",
            prompt="Build something",
        )
    )
    assert result == "pipeline-result"

    m = _read_manifest(str(tmp_path))
    assert re.fullmatch(r"[0-9a-f]{32}", m["job_id"])
    assert m["status"] == "completed"
    assert m["pipeline"] == "custom"
    assert m["prompt"] == "Build something"
    assert m["error"] is None
    assert m["workspace"] == str(tmp_path)
    assert m["started_at"] <= m["finished_at"]
    assert m["journaled_by"] == "cli"


def test_manifest_failed_on_exception(tmp_path):
    async def fake_pipeline():
        raise PipelineError("boom at code stage")

    with pytest.raises(PipelineError, match="boom"):
        asyncio.run(
            run_cli_journaled(
                fake_pipeline,
                workspace=str(tmp_path),
                pipeline="sequential",
                prompt="p",
            )
        )
    m = _read_manifest(str(tmp_path))
    assert m["status"] == "failed"
    assert "boom at code stage" in m["error"]
    assert m["finished_at"] is not None


def test_collab_log_refreshed_after_completion(tmp_path):
    """The ordering fix: the pipeline exports the log while the manifest still
    says "running"; the CLI wrapper's final journal must re-export it so the
    shipped log carries the terminal status and the job id."""
    ws = str(tmp_path)
    raw = os.path.join(ws, ".genius", "slug", "logs", "raw")
    os.makedirs(raw)
    with open(os.path.join(raw, "final_review.md"), "w", encoding="utf-8") as fh:
        fh.write("review")

    async def fake_pipeline():
        # Mid-run export, exactly like _emit_hackathon_artifacts does.
        with open(
            os.path.join(ws, "ai_collaboration_log.md"), "w", encoding="utf-8"
        ) as fh:
            fh.write(export_collab_log(ws))
        mid_log = open(
            os.path.join(ws, "ai_collaboration_log.md"), encoding="utf-8"
        ).read()
        assert "| Status | running |" in mid_log
        return "ok"

    asyncio.run(
        run_cli_journaled(
            fake_pipeline, workspace=ws, pipeline="custom", prompt="p"
        )
    )

    final_log = open(
        os.path.join(ws, "ai_collaboration_log.md"), encoding="utf-8"
    ).read()
    m = _read_manifest(ws)
    assert "| Status | completed |" in final_log
    assert m["job_id"] in final_log


def test_no_refresh_when_log_absent(tmp_path):
    async def fake_pipeline():
        return "ok"

    asyncio.run(
        run_cli_journaled(
            fake_pipeline, workspace=str(tmp_path), pipeline="e2e", prompt="p"
        )
    )
    # refresh_log_if_present never CREATES the log.
    assert not os.path.exists(os.path.join(str(tmp_path), "ai_collaboration_log.md"))
    assert _read_manifest(str(tmp_path))["status"] == "completed"


def test_manifest_write_failure_is_swallowed(tmp_path):
    """Journaling is best-effort: an unwritable workspace path must not fail
    the pipeline run itself."""
    blocker = tmp_path / "not_a_dir"
    blocker.write_text("file, not dir")

    async def fake_pipeline():
        return "still-ran"

    result = asyncio.run(
        run_cli_journaled(
            fake_pipeline, workspace=str(blocker), pipeline="custom", prompt="p"
        )
    )
    assert result == "still-ran"


def test_workspace_defaults_to_cwd(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)

    async def fake_pipeline():
        return "ok"

    asyncio.run(
        run_cli_journaled(fake_pipeline, workspace=None, pipeline="custom", prompt="p")
    )
    m = _read_manifest(str(tmp_path))
    assert m["workspace"] == str(tmp_path)
