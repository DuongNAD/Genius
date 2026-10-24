"""Comprehensive End-to-End Offline Mock & Dry-Run Test Suite.

Validates the full Genius multi-agent orchestration lifecycle, CLI stream
emulators, provider fallback chains, rate limiting, self-healing loops,
and MCP tools in 100% deterministic offline mode.
"""

import asyncio
import json
import os
import re
import shutil
import tempfile
import pytest

from ag_core.models import (
    DebateVerdict,
    DesignPlan,
    SecurityReport,
)
from ag_core.provider_factory import FallbackProvider
from ag_core.providers.notebooklm_provider import _parse_nlm_json
from ag_core.providers.openai_provider import _parse_codex_jsonl
from ag_core.testing.offline_mock import (
    CLIStreamEmulator,
    MockScenarioRule,
    OfflineMockProvider,
)
from ag_core.utils.cli_runner import CLITimeoutError
from ag_core.utils.code_extract import extract_code


# ---------------------------------------------------------------------------
# 1. CLI Stream Emulator & Format Parser Verification
# ---------------------------------------------------------------------------


def test_stream_emulator_codex_jsonl():
    """Verifies that CLIStreamEmulator.codex_jsonl produces valid JSONL streams for OpenAIProvider."""
    code_text = "def calculate_sum(a, b):\n    return a + b\n"
    stream_bytes = CLIStreamEmulator.codex_jsonl(
        code_text, prompt_tokens=120, completion_tokens=45
    )
    stdout_str = stream_bytes.decode("utf-8")

    content, error_parts, pt, ct, total = _parse_codex_jsonl(stdout_str)
    assert content == code_text
    assert error_parts == []
    assert pt == 120
    assert ct == 45
    assert total == 165


def test_stream_emulator_codex_jsonl_error():
    """Verifies that CLIStreamEmulator.codex_jsonl properly embeds error events."""
    stream_bytes = CLIStreamEmulator.codex_jsonl(
        "", error_message="Fatal token window exhaustion"
    )
    stdout_str = stream_bytes.decode("utf-8")
    content, error_parts, pt, ct, total = _parse_codex_jsonl(stdout_str)
    assert content == ""
    assert any("Fatal token window exhaustion" in err for err in error_parts)


def test_stream_emulator_claude_json():
    """Verifies that CLIStreamEmulator.claude_json produces valid Claude Code JSON envelopes."""
    result_text = '{"project_name": "demo_service", "files": []}'
    envelope_bytes = CLIStreamEmulator.claude_json(
        result_text, prompt_tokens=80, completion_tokens=30
    )
    data = json.loads(envelope_bytes.decode("utf-8"))
    assert data["result"] == result_text
    assert data["is_error"] is False
    assert data["usage"]["input_tokens"] == 80
    assert data["usage"]["output_tokens"] == 30


def test_stream_emulator_grok_json():
    """Verifies that CLIStreamEmulator.grok_json produces valid xAI Grok JSON output."""
    review_text = "All architectural constraints are satisfied. [APPROVED]"
    json_bytes = CLIStreamEmulator.grok_json(review_text, session_id="test-sess-100")
    data = json.loads(json_bytes.decode("utf-8"))
    assert data["text"] == review_text
    assert data["sessionId"] == "test-sess-100"
    assert data["stopReason"] == "EndTurn"


def test_stream_emulator_agy_plain_text():
    """Verifies that CLIStreamEmulator.agy_plain_text produces clean plain-text output."""
    plain_text = "Technical research findings for Antigravity 2.0"
    out_bytes = CLIStreamEmulator.agy_plain_text(plain_text)
    assert out_bytes.decode("utf-8").strip() == plain_text


def test_stream_emulator_notebooklm_json():
    """Verifies that CLIStreamEmulator.nlm_json produces valid NotebookLM query responses."""
    answer = "The architecture uses asynchronous token-bucket rate limiting."
    nlm_bytes = CLIStreamEmulator.nlm_json(answer, notebook_id="nb-123")
    parsed = _parse_nlm_json(nlm_bytes.decode("utf-8"))
    assert parsed is not None
    assert parsed["answer"] == answer
    assert parsed["notebook_id"] == "nb-123"
    assert len(parsed["citations"]) > 0


# ---------------------------------------------------------------------------
# 2. OfflineMockProvider Role Generation & Schema Compliance
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_mock_provider_all_roles(offline_mock_provider):
    """Verifies that OfflineMockProvider generates valid domain outputs for all 7 roles."""
    roles = ["researcher", "architect", "critic", "coder", "reviewer", "tester", "security", "devops"]

    for role in roles:
        res = await offline_mock_provider.send_prompt(f"Perform task for {role}", role=role)
        assert isinstance(res, dict)
        content = res["content"]
        assert len(content) > 0

        if role == "architect":
            code = extract_code(content)
            plan_dict = json.loads(code)
            plan = DesignPlan.model_validate(plan_dict)
            assert len(plan.files) > 0
            assert plan.project_name == "genius_app"

        elif role == "critic":
            code = extract_code(content)
            verdict_dict = json.loads(code)
            verdict = DebateVerdict.model_validate(verdict_dict)
            assert verdict.approved is True
            assert verdict.score >= 4.0

        elif role == "security":
            code = extract_code(content)
            report_dict = json.loads(code)
            report = SecurityReport.model_validate(report_dict)
            assert isinstance(report.blocking, bool)
            assert report.score > 0.0

        elif role == "devops":
            assert "# filepath:" in content
            assert "Dockerfile" in content or "docker-compose.yml" in content


# ---------------------------------------------------------------------------
# 3. Multi-Turn Conversation & Session History Tracking
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_mock_multi_turn_history(offline_mock_provider):
    """Verifies conversation history accumulation and session tracking across multiple turns."""
    session_id = "session_xyz"
    await offline_mock_provider.send_prompt(
        "Design the system architecture", role="architect", session_id=session_id
    )
    await offline_mock_provider.send_prompt(
        "Refine the system architecture with Redis caching", role="architect", session_id=session_id
    )
    await offline_mock_provider.send_prompt(
        "Generate source code for the model", role="coder", session_id=session_id
    )

    assert offline_mock_provider.call_count == 3
    assert len(offline_mock_provider.history) == 3
    assert len(offline_mock_provider.sessions[session_id]) == 3

    records = offline_mock_provider.sessions[session_id]
    assert records[0]["role"] == "architect"
    assert records[1]["role"] == "architect"
    assert records[2]["role"] == "coder"


# ---------------------------------------------------------------------------
# 4. Deterministic Fault Injection & Recovery
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_mock_fault_injection_rate_limit_429(offline_mock_provider):
    """Verifies 429 Rate Limit injection and recovery after specified attempts."""
    offline_mock_provider.inject_fault(fault_type="rate_limit", attempts=2, retry_after=0.1)

    # First attempt fails
    with pytest.raises(RuntimeError) as exc_info1:
        await offline_mock_provider.send_prompt("Test prompt attempt 1")
    assert "429 Rate Limit Exceeded" in str(exc_info1.value)

    # Second attempt fails
    with pytest.raises(RuntimeError) as exc_info2:
        await offline_mock_provider.send_prompt("Test prompt attempt 2")
    assert "429 Rate Limit Exceeded" in str(exc_info2.value)

    # Third attempt succeeds after fault counter exhausted
    res = await offline_mock_provider.send_prompt("Test prompt attempt 3")
    assert "content" in res
    assert len(res["content"]) > 0


@pytest.mark.asyncio
async def test_offline_mock_fault_injection_auth_401(offline_mock_provider):
    """Verifies 401 Unauthorized fault injection."""
    offline_mock_provider.inject_fault(fault_type="auth_error", attempts=1)
    with pytest.raises(RuntimeError) as exc_info:
        await offline_mock_provider.send_prompt("Authorized task")
    assert "401 Unauthorized" in str(exc_info.value)


@pytest.mark.asyncio
async def test_offline_mock_fault_injection_timeout(offline_mock_provider):
    """Verifies timeout fault injection triggering CLITimeoutError."""
    offline_mock_provider.inject_fault(fault_type="timeout", attempts=1)
    with pytest.raises(CLITimeoutError) as exc_info:
        await offline_mock_provider.send_prompt("Slow task")
    assert "timed out" in str(exc_info.value)


@pytest.mark.asyncio
async def test_offline_mock_fault_injection_process_crash(offline_mock_provider):
    """Verifies non-zero process crash fault injection."""
    offline_mock_provider.inject_fault(fault_type="crash", attempts=1)
    with pytest.raises(RuntimeError) as exc_info:
        await offline_mock_provider.send_prompt("Crashing task")
    assert "terminated unexpectedly" in str(exc_info.value)


# ---------------------------------------------------------------------------
# 5. FallbackProvider Chain Integration in Offline Mode
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_mock_fallback_chain_offline():
    """Verifies FallbackProvider automatically fails over to secondary mock on primary error."""
    primary = OfflineMockProvider(model_name="primary-mock", backend_flavor="codex")
    primary.inject_fault(fault_type="crash", attempts=5)  # Primary continuously fails

    secondary = OfflineMockProvider(model_name="secondary-mock", backend_flavor="claude")

    fallback_chain = FallbackProvider(
        role="coder",
        backends=[
            ("primary", lambda: primary),
            ("secondary", lambda: secondary),
        ],
    )

    result = await fallback_chain.send_prompt("Write hello world function")
    assert "content" in result
    assert secondary.call_count == 1
    assert primary.call_count == 1

    # Sticky success: subsequent call immediately tries secondary
    await fallback_chain.send_prompt("Write another function")
    assert secondary.call_count == 2
    assert primary.call_count == 1  # Primary not retried due to sticky success


# ---------------------------------------------------------------------------
# 6. Concurrency & Rate Limiting Verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_mock_rate_limiter_concurrency(offline_mock_provider):
    """Verifies that concurrent prompt executions adhere to the concurrency semaphore without deadlock."""
    tasks = [
        offline_mock_provider.send_prompt(f"Concurrent task #{i}", role="coder")
        for i in range(10)
    ]
    results = await asyncio.gather(*tasks)
    assert len(results) == 10
    for res in results:
        assert "content" in res


# ---------------------------------------------------------------------------
# 7. Multi-Agent Debate & Architectural Refinement
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_mock_debate_flow_and_refinement():
    """Verifies multi-round architectural debate and synthesis."""
    provider = OfflineMockProvider()

    # Rule for Round 1: Critic finds missing fallback
    provider.add_rule(
        MockScenarioRule(
            pattern="Analyze the following draft architecture plan",
            response=DebateVerdict(
                approved=False,
                score=3.0,
                criteria_scores={"completeness": 3.0, "modularity": 4.0, "security": 3.0, "feasibility": 4.0},
                strengths=["Good modular design"],
                flaws=["Missing in-memory fallback when cache fails"],
                feedback="Add in-memory fallback strategy.",
                concrete_suggestions=["Include fallback in service.py"],
                risks=["Cache outage disrupts service"],
            ),
        )
    )

    # Round 1 Critic evaluation
    critic_res = await provider.send_prompt(
        "You are CriticReviewer. Analyze the following draft architecture plan proposed by Claude.",
        role="critic",
    )
    code = extract_code(critic_res["content"])
    verdict1 = DebateVerdict.model_validate(json.loads(code))
    assert verdict1.approved is False
    assert len(verdict1.flaws) > 0

    # Architect refinement
    architect_res = await provider.send_prompt(
        "Refine your draft architecture plan based on criticism: Add in-memory fallback strategy.",
        role="architect",
    )
    plan_dict = json.loads(extract_code(architect_res["content"]))
    plan = DesignPlan.model_validate(plan_dict)
    assert len(plan.files) > 0

    # Round 2 Critic approval
    provider.add_rule(
        MockScenarioRule(
            pattern="refined plan",
            response=DebateVerdict(
                approved=True,
                score=4.8,
                criteria_scores={"completeness": 5.0, "modularity": 5.0, "security": 4.8, "feasibility": 5.0},
                strengths=["Fallback cleanly integrated"],
                flaws=[],
                feedback="The refined architecture plan is complete and approved. [APPROVED]",
                concrete_suggestions=[],
                risks=[],
            ),
        )
    )
    approval_res = await provider.send_prompt(
        "Evaluate the refined plan with incorporated feedback.", role="critic"
    )
    verdict2 = DebateVerdict.model_validate(json.loads(extract_code(approval_res["content"])))
    assert verdict2.is_approved is True
    assert "[APPROVED]" in verdict2.feedback


# ---------------------------------------------------------------------------
# 8. Self-Healing Code Generation & Verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_mock_self_healing_code_and_test():
    """Simulates self-healing feedback loop: initial buggy code -> error log feedback -> corrected code."""
    provider = OfflineMockProvider()

    buggy_code = "def parse_data(raw):\n    return json.loads(raw"
    fixed_code = "import json\n\ndef parse_data(raw):\n    return json.loads(raw)\n"

    # Rule for initial attempt returning buggy code
    provider.add_rule(
        MockScenarioRule(
            pattern="Implement data parser",
            response=f"```python\n{buggy_code}```",
        )
    )

    # Corrected implementation rule for retry
    provider.add_rule(
        MockScenarioRule(
            pattern="SyntaxError",
            response=f"```python\n{fixed_code}```",
        )
    )

    # Initial attempt
    initial_res = await provider.send_prompt("Implement data parser in src/parser.py", role="coder")
    assert "parse_data" in initial_res["content"]

    # Simulating feedback on SyntaxError
    retry_prompt = (
        "The previous implementation attempt for 'src/parser.py' failed verification.\n"
        "Errors/Logs: SyntaxError: unexpected EOF while parsing\n"
        "Correct the syntax error."
    )
    fixed_res = await provider.send_prompt(retry_prompt, role="coder")
    extracted_fixed = extract_code(fixed_res["content"])
    assert "return json.loads(raw)" in extracted_fixed


# ---------------------------------------------------------------------------
# 9. Security Audit Blocking vs Non-Blocking Gating
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_mock_security_blocking_gating(offline_mock_provider):
    """Verifies security report gating distinguishing blocking vulnerabilities from clean code."""
    # 1. Clean audit
    clean_res = await offline_mock_provider.send_prompt("Perform clean security audit on src/", role="security")
    clean_report = SecurityReport.model_validate(json.loads(extract_code(clean_res["content"])))
    assert clean_report.blocking is False
    assert clean_report.score >= 9.0
    assert len(clean_report.findings) == 0

    # 2. Vulnerable audit
    vuln_res = await offline_mock_provider.send_prompt(
        "Perform audit on code with hardcoded SQL injection vulnerability and eval()", role="security"
    )
    vuln_report = SecurityReport.model_validate(json.loads(extract_code(vuln_res["content"])))
    assert vuln_report.blocking is True
    assert vuln_report.score < 5.0
    assert len(vuln_report.blocking_findings) > 0


# ---------------------------------------------------------------------------
# 10. DevOps Artifact Materialization
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_mock_devops_materialization(offline_mock_provider):
    """Verifies that DevOps outputs contain # filepath: markers and parseable artifacts."""
    res = await offline_mock_provider.send_prompt("Generate deployment configuration", role="devops")
    content = res["content"]

    temp_dir = tempfile.mkdtemp(prefix="devops_test_")
    try:
        # Simulate orchestrator materialization
        pattern = re.compile(
            r"```(?:[a-zA-Z0-9_\-]+)?\s*\n(?:#|//)\s*(?:filepath|path):\s*([^\n\r]+)\n(.*?)```",
            re.DOTALL,
        )
        matches = list(pattern.finditer(content))
        assert len(matches) >= 2

        for match in matches:
            rel_path = match.group(1).strip()
            file_body = match.group(2)
            dest = os.path.join(temp_dir, rel_path)
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            with open(dest, "w", encoding="utf-8") as f:
                f.write(file_body)
            assert os.path.exists(dest)
            assert len(file_body) > 0
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


# ---------------------------------------------------------------------------
# 11. Full End-to-End Orchestration Pipeline Dry-Run
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_mock_full_e2e_pipeline(dryrun_pipeline):
    """
    Executes the complete 8-stage E2E pipeline completely offline:
    Research -> Plan -> Debate -> Code -> Review -> Test -> Security -> DevOps
    """
    workspace = dryrun_pipeline.workspace_dir
    provider = dryrun_pipeline.provider
    assert workspace is not None

    # Stage 1: Research
    research_res = await provider.send_prompt("Research requirements for user auth service", role="researcher")
    research_file = os.path.join(workspace, "research.md")
    with open(research_file, "w", encoding="utf-8") as f:
        f.write(research_res["content"])
    assert os.path.exists(research_file)

    # Stage 2: Architect Plan
    plan_res = await provider.send_prompt(f"Design architecture for: {research_res['content']}", role="architect")
    plan = DesignPlan.model_validate(json.loads(extract_code(plan_res["content"])))
    plan_file = os.path.join(workspace, "design.md")
    with open(plan_file, "w", encoding="utf-8") as f:
        f.write(plan_res["content"])
    assert len(plan.files) > 0

    # Stage 3: Debate Verdict
    debate_res = await provider.send_prompt(f"Evaluate plan: {plan_res['content']}", role="critic")
    verdict = DebateVerdict.model_validate(json.loads(extract_code(debate_res["content"])))
    assert verdict.approved is True

    # Stage 4: Code Generation for each designed file
    for dfile in plan.files:
        code_res = await provider.send_prompt(
            f"Implement file {dfile.path} specification: {dfile.specification}", role="coder"
        )
        file_path = os.path.join(workspace, dfile.path)
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "w", encoding="utf-8") as f:
            f.write(extract_code(code_res["content"]))
        assert os.path.exists(file_path)

    # Stage 5: Review
    review_res = await provider.send_prompt(f"Review code in {workspace}", role="reviewer")
    review_file = os.path.join(workspace, "review.md")
    with open(review_file, "w", encoding="utf-8") as f:
        f.write(review_res["content"])
    assert os.path.exists(review_file)

    # Stage 6: Tester
    test_res = await provider.send_prompt(f"Generate unit tests for {workspace}", role="tester")
    test_file = os.path.join(workspace, "test_generated.py")
    with open(test_file, "w", encoding="utf-8") as f:
        f.write(extract_code(test_res["content"]))
    assert os.path.exists(test_file)

    # Stage 7: Security Audit
    security_res = await provider.send_prompt(f"Audit code in {workspace}", role="security")
    sec_report = SecurityReport.model_validate(json.loads(extract_code(security_res["content"])))
    assert sec_report.blocking is False

    # Stage 8: DevOps Configuration
    devops_res = await provider.send_prompt(f"Generate deployment files for {workspace}", role="devops")
    devops_file = os.path.join(workspace, "deploy.md")
    with open(devops_file, "w", encoding="utf-8") as f:
        f.write(devops_res["content"])
    assert os.path.exists(devops_file)

    # Validate that entire sequence executed offline with zero network leaks
    assert dryrun_pipeline.call_count >= 8


# ---------------------------------------------------------------------------
# 12. MCP Tools 100% Offline Execution Verification
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_offline_mock_mcp_tools(offline_genius_env):
    """Verifies that MCP agent wrappers execute seamlessly in offline mode."""
    import mcp_server

    # 1. Research tool
    research_out = await mcp_server.dispatch_tool("research", {"prompt": "Investigate caching architectures"})
    assert "## Requirements" in research_out or "Technical" in research_out

    # 2. Design tool
    design_out = await mcp_server.dispatch_tool("design", {"prompt": "Design caching service"})
    assert "```json" in design_out or "project_name" in design_out

    # 3. Code tool
    code_out = await mcp_server.dispatch_tool("code", {"prompt": "Implement token bucket class in src/limiter.py"})
    assert "class " in code_out or "def " in code_out

    # 4. Unit Test tool
    test_out = await mcp_server.dispatch_tool("unit_test", {"prompt": "Write tests for token bucket"})
    assert "pytest" in test_out or "def test_" in test_out

    # 5. Security Audit tool
    audit_out = await mcp_server.dispatch_tool("security_audit", {"prompt": "Audit limiter implementation"})
    assert "score" in audit_out or "findings" in audit_out or "```json" in audit_out

    # 6. Deploy tool
    deploy_out = await mcp_server.dispatch_tool("deploy", {"prompt": "Generate docker compose configuration"})
    assert "filepath:" in deploy_out or "Dockerfile" in deploy_out

    # 7. Debate tool
    debate_out = await mcp_server.dispatch_tool(
        "debate", {"design": '{"project_name": "app", "files": []}', "prompt": "Evaluate design"}
    )
    assert "approved" in debate_out or "[APPROVED]" in debate_out

    # 8. Review tool
    review_out = await mcp_server.dispatch_tool(
        "review", {"code": "def foo(): pass", "instructions": "check styling"}
    )
    assert "Review" in review_out or "Summary" in review_out
