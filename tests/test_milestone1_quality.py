"""Unit and integration tests for Milestone 1 Prompt & Agent Quality features.

Validates:
- Pydantic models (SecurityFinding, SecurityReport, DevOpsArtifact, DevOpsPlan, DebateVerdict)
- DevOps file materialization from '# filepath:' code blocks and DevOpsPlan JSON
- Structured DebateVerdict parsing and [APPROVED] backward compatibility
- Centralized prompt templates, polyglot coder prompt, and few-shot exemplars
- MCP server debate with structured verdict and consensus synthesis
"""

import json
import os
import pytest
from unittest.mock import AsyncMock, patch

from ag_core.models import (
    DesignFile,
    DesignPlan,
    SecurityFinding,
    SecurityReport,
    DevOpsArtifact,
    DevOpsPlan,
    DebateVerdict,
)
from ag_core.utils.prompt_templates import (
    AGENT_CORE_RULES,
    CODER_PROMPT,
    RESEARCHER_PROMPT,
    ARCHITECT_PROMPT,
    TESTER_PROMPT,
    SECURITY_PROMPT,
    DEVOPS_PROMPT,
    CRITIC_DEBATE_PROMPT,
    ARCHITECT_REFINE_PROMPT,
    DEBATE_SYNTHESIS_PROMPT,
    CODEX_RETRY_PROMPT,
    TESTER_RETRY_PROMPT,
    RESEARCHER_FEW_SHOT,
    ARCHITECT_FEW_SHOT,
    CODER_FEW_SHOT,
    TESTER_FEW_SHOT,
    SECURITY_FEW_SHOT_CLEAN,
    SECURITY_FEW_SHOT_VULNERABLE,
    DEVOPS_FEW_SHOT,
    CRITIC_FEW_SHOT_CRITIQUE,
    CRITIC_FEW_SHOT_APPROVAL,
    JUDGE_CALIBRATION_ANCHORS,
)
from ag_core.agents.claude_architect import _architect_system_prompt
from ag_core.eval.judge import JUDGE_SYSTEM_PROMPT
from orchestrator import (
    parse_devops_artifacts,
    materialize_devops_artifacts,
    parse_debate_verdict,
    is_debate_approved,
)


# ==============================================================================
# 1. Pydantic Models Validation
# ==============================================================================


def test_pydantic_design_models():
    f = DesignFile(path="src/main.py", specification="Main app entry point")
    assert f.path == "src/main.py"
    assert f.description == ""

    plan = DesignPlan(
        project_name="my_api",
        description="REST service",
        files=[f],
        dependencies=["fastapi", "uvicorn"],
    )
    assert len(plan.files) == 1
    assert plan.dependencies == ["fastapi", "uvicorn"]


def test_pydantic_security_models():
    finding = SecurityFinding(
        severity="critical",
        category="Injection",
        line=42,
        issue="eval() on unsanitized user input",
        fix="Use ast.literal_eval",
    )
    assert finding.is_blocking is True
    assert finding.severity == "critical"

    report = SecurityReport(
        blocking=True,
        score=3.5,
        summary="Found 1 critical issue",
        findings=[finding],
    )
    assert len(report.critical_findings) == 1
    assert len(report.high_findings) == 0
    assert len(report.blocking_findings) == 1

    clean_report = SecurityReport(
        blocking=False,
        score=10.0,
        summary="Clean codebase",
        findings=[],
    )
    assert len(clean_report.blocking_findings) == 0
    assert clean_report.blocking is False


def test_pydantic_devops_models():
    art = DevOpsArtifact(
        path="Dockerfile",
        content="FROM python:3.11-slim\nWORKDIR /app",
        description="Base Dockerfile",
    )
    assert art.path == "Dockerfile"
    assert "WORKDIR" in art.content

    plan = DevOpsPlan(
        summary="Containerized deployment plan",
        artifacts=[art],
    )
    assert len(plan.artifacts) == 1
    assert plan.artifacts[0].path == "Dockerfile"


def test_pydantic_debate_verdict_models():
    v_approved = DebateVerdict(
        approved=True,
        score=4.8,
        criteria_scores={"completeness": 5.0, "security": 4.5},
        strengths=["Solid modularity"],
        flaws=[],
    )
    assert v_approved.is_approved is True
    assert v_approved.score == 4.8

    v_flawed = DebateVerdict(
        approved=False,
        score=3.0,
        criteria_scores={"completeness": 3.0, "security": 2.0},
        strengths=["Good idea"],
        flaws=["Missing auth"],
        concrete_suggestions=["Add JWT middleware"],
    )
    assert v_flawed.is_approved is False
    assert len(v_flawed.flaws) == 1


# ==============================================================================
# 2. DevOps Parsing & Materialization Tests
# ==============================================================================


def test_parse_devops_artifacts_from_filepath_blocks():
    devops_text = (
        "Deployment plan documentation.\n\n"
        "```dockerfile\n"
        "# filepath: Dockerfile\n"
        "FROM python:3.11\n"
        "WORKDIR /app\n"
        "```\n\n"
        "```yaml\n"
        "# filepath: docker-compose.yml\n"
        "version: '3.8'\n"
        "services:\n"
        "  web:\n"
        "    build: .\n"
        "```\n\n"
        "```yaml\n"
        "# filepath: .github/workflows/ci.yml\n"
        "name: CI\n"
        "on: [push]\n"
        "```\n"
    )
    artifacts = parse_devops_artifacts(devops_text)
    assert len(artifacts) == 3
    paths = [a.path for a in artifacts]
    assert "Dockerfile" in paths
    assert "docker-compose.yml" in paths
    assert ".github/workflows/ci.yml" in paths


def test_parse_devops_artifacts_from_json_plan():
    plan_dict = {
        "summary": "Containerized setup",
        "artifacts": [
            {"path": "Dockerfile", "content": "FROM alpine\nCMD echo hi"},
            {"path": "deploy.sh", "content": "#!/bin/bash\necho deploy"},
        ],
    }
    devops_text = f"```json\n{json.dumps(plan_dict)}\n```"
    artifacts = parse_devops_artifacts(devops_text)
    assert len(artifacts) == 2
    assert artifacts[0].path == "Dockerfile"
    assert artifacts[1].path == "deploy.sh"


def test_materialize_devops_artifacts(tmp_path):
    project_dir = str(tmp_path / "my_project")
    os.makedirs(project_dir, exist_ok=True)

    devops_text = (
        "```dockerfile\n"
        "# filepath: Dockerfile\n"
        "FROM python:3.11\n"
        "COPY . /app\n"
        "```\n\n"
        "```yaml\n"
        "# filepath: nested/config.yaml\n"
        "app:\n"
        "  name: test\n"
        "```\n"
    )
    written = materialize_devops_artifacts(devops_text, project_dir)
    assert len(written) == 2

    dockerfile_path = os.path.join(project_dir, "Dockerfile")
    assert os.path.exists(dockerfile_path)
    with open(dockerfile_path, "r", encoding="utf-8") as f:
        assert "FROM python:3.11" in f.read()

    nested_config_path = os.path.join(project_dir, "nested", "config.yaml")
    assert os.path.exists(nested_config_path)
    with open(nested_config_path, "r", encoding="utf-8") as f:
        assert "name: test" in f.read()


def test_materialize_devops_traversal_protection(tmp_path):
    project_dir = str(tmp_path / "my_project")
    os.makedirs(project_dir, exist_ok=True)

    malicious_text = (
        "```text\n"
        "# filepath: ../../secret.txt\n"
        "malicious content\n"
        "```\n"
    )
    written = materialize_devops_artifacts(malicious_text, project_dir)
    assert len(written) == 0
    assert not os.path.exists(os.path.join(tmp_path, "secret.txt"))


# ==============================================================================
# 3. Debate Verdict Parsing Tests
# ==============================================================================


def test_parse_debate_verdict_structured_json():
    critic_output = (
        "Here is my architectural review:\n"
        "```json\n"
        "{\n"
        '  "approved": true,\n'
        '  "score": 4.8,\n'
        '  "criteria_scores": {"completeness": 5.0, "modularity": 4.5},\n'
        '  "strengths": ["Clean separation of concerns"],\n'
        '  "flaws": [],\n'
        '  "feedback": "Ready for implementation."\n'
        "}\n"
        "```\n"
    )
    verdict = parse_debate_verdict(critic_output)
    assert verdict is not None
    assert verdict.approved is True
    assert verdict.score == 4.8
    assert is_debate_approved(critic_output) is True


def test_parse_debate_verdict_rejection_json():
    critic_output = (
        "```json\n"
        "{\n"
        '  "approved": false,\n'
        '  "score": 2.5,\n'
        '  "flaws": ["Missing database persistence layer"],\n'
        '  "concrete_suggestions": ["Add SQLite repository"]\n'
        "}\n"
        "```\n"
    )
    verdict = parse_debate_verdict(critic_output)
    assert verdict is not None
    assert verdict.approved is False
    assert is_debate_approved(critic_output) is False


def test_debate_approved_fallback_string():
    assert is_debate_approved("The plan looks complete and robust. [APPROVED]") is True
    assert is_debate_approved("The plan requires more work on caching.") is False
    assert is_debate_approved("") is False


def test_debate_structured_verdict_overrides_string():
    # If JSON explicitly says approved: false, presence of [APPROVED] in markdown prose does not override it
    critic_output = (
        "The plan cannot be [APPROVED] until caching is addressed.\n"
        "```json\n"
        "{\n"
        '  "approved": false,\n'
        '  "score": 2.0,\n'
        '  "flaws": ["No caching"]\n'
        "}\n"
        "```\n"
    )
    assert is_debate_approved(critic_output) is False


# ==============================================================================
# 4. Prompt Templates & Few-Shot Library Tests
# ==============================================================================


def test_coder_prompt_polyglot():
    assert "```python, ```yaml, ```json, ```dockerfile" in CODER_PROMPT
    assert "Do NOT run tests, commands, or tools" in CODER_PROMPT
    assert "No explanations, no prose" in CODER_PROMPT


def test_prompt_templates_contain_exemplars():
    assert "Do not read, parse, or echo `.env` files" in AGENT_CORE_RULES
    assert "Separate planning from implementation" in ARCHITECT_PROMPT
    assert "Technical Discovery Brief" in RESEARCHER_PROMPT
    assert "TokenBucketLimiter" in TESTER_PROMPT
    assert "Code Injection (CWE-95)" in SECURITY_PROMPT
    assert "# filepath: Dockerfile" in DEVOPS_PROMPT
    assert "Score Calibration Anchors" in JUDGE_SYSTEM_PROMPT

    assert "redis.asyncio" in RESEARCHER_FEW_SHOT
    assert "rate_limiter_service" in ARCHITECT_FEW_SHOT
    assert "InMemoryTokenBucket" in CODER_FEW_SHOT
    assert "test_fallback_on_redis_connection_error" in TESTER_FEW_SHOT
    assert '"blocking": false' in SECURITY_FEW_SHOT_CLEAN
    assert '"blocking": true' in SECURITY_FEW_SHOT_VULNERABLE
    assert "FROM python:3.11-slim" in DEVOPS_FEW_SHOT
    assert '"approved": false' in CRITIC_FEW_SHOT_CRITIQUE
    assert '"approved": true' in CRITIC_FEW_SHOT_APPROVAL
    assert "Score 5 (Production-Ready)" in JUDGE_CALIBRATION_ANCHORS


def test_architect_system_prompt_multi_tier():
    sys_prompt = _architect_system_prompt()
    assert "rate_limiter_service" in sys_prompt
    assert "src/config.py" in sys_prompt
    assert "src/services/limiter_service.py" in sys_prompt
    assert "src/routers/limiter_router.py" in sys_prompt


def test_debate_and_retry_templates():
    critic_p = CRITIC_DEBATE_PROMPT.format(draft_plan="plan v1", context="ctx")
    assert "You are CriticReviewer" in critic_p
    assert "plan v1" in critic_p

    refine_p = ARCHITECT_REFINE_PROMPT.format(
        draft_plan="plan v1", criticism="fix auth", context="ctx"
    )
    assert "You are Claude, the architect agent" in refine_p
    assert "fix auth" in refine_p

    synth_p = DEBATE_SYNTHESIS_PROMPT.format(
        draft_plan="plan v2", criticism="final notes", context="ctx"
    )
    assert "consensus synthesis" in synth_p
    assert "final notes" in synth_p

    codex_retry = CODEX_RETRY_PROMPT.format(
        file_path="src/main.py",
        exit_code=1,
        error_logs="AssertionError",
        security_report="None",
    )
    assert "src/main.py" in codex_retry
    assert "AssertionError" in codex_retry

    tester_retry = TESTER_RETRY_PROMPT.format(
        file_path="tests/test_app.py",
        exit_code=1,
        error_logs="ImportError",
    )
    assert "tests/test_app.py" in tester_retry
    assert "ImportError" in tester_retry


# ==============================================================================
# 5. MCP Server Debate Flow with Structured Verdict
# ==============================================================================


@pytest.mark.asyncio
async def test_mcp_server_run_debate_instant_structured_approval():
    from mcp_server import _run_debate

    approved_verdict = (
        "```json\n"
        '{"approved": true, "score": 4.9, "strengths": ["Excellent design"]}\n'
        "```\n"
        "[APPROVED]"
    )

    with patch("mcp_server.execute_agent", new_callable=AsyncMock) as mock_agent:
        mock_agent.return_value = approved_verdict
        res_json = await _run_debate("draft design", "build a service", rounds=2)
        data = json.loads(res_json)
        assert data["approved"] is True
        assert len(data["rounds"]) == 1
        assert data["rounds"][0]["verdict"]["approved"] is True


@pytest.mark.asyncio
async def test_mcp_server_run_debate_max_rounds_consensus_synthesis():
    from mcp_server import _run_debate

    criticism = (
        "```json\n"
        '{"approved": false, "score": 3.0, "flaws": ["Need caching"]}\n'
        "```"
    )
    synthesized_design = "Synthesized final design v2"

    responses = [criticism, "refined v1", criticism, synthesized_design]
    call_idx = 0

    async def mock_execute(role, prompt, ctx):
        nonlocal call_idx
        resp = responses[call_idx]
        call_idx += 1
        return resp

    with patch("mcp_server.execute_agent", side_effect=mock_execute):
        res_json = await _run_debate("draft design", "build a service", rounds=2)
        data = json.loads(res_json)
        assert data["approved"] is False
        assert len(data["rounds"]) == 2
        assert data["rounds"][1]["synthesized"] is True
        assert data["design"] == synthesized_design
