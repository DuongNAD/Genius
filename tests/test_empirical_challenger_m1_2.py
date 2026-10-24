"""Empirical Challenger Test Suite for Milestone 1:
Debate Deliberation, Consensus Synthesis, Scoring Scenarios, and Few-Shot Prompt Formatting across all 7 Agent Classes.
"""

import ast
import json
import os
import pytest
import unittest.mock as mock
import yaml

from ag_core.models import (
    DesignPlan,
    DebateVerdict,
    SecurityReport,
)
from ag_core.utils.prompt_templates import (
    AGENT_CORE_RULES,
    ARCHITECT_FEW_SHOT,
    CODER_FEW_SHOT,
    CODER_PROMPT,
    TESTER_FEW_SHOT,
    TESTER_PROMPT,
    SECURITY_FEW_SHOT_CLEAN,
    SECURITY_FEW_SHOT_VULNERABLE,
    DEVOPS_FEW_SHOT,
    CRITIC_FEW_SHOT_CRITIQUE,
    CRITIC_FEW_SHOT_APPROVAL,
    CRITIC_DEBATE_PROMPT,
    ARCHITECT_REFINE_PROMPT,
    DEBATE_SYNTHESIS_PROMPT,
    CODEX_RETRY_PROMPT,
    TESTER_RETRY_PROMPT,
)
from ag_core.agents.claude_architect import ClaudeArchitectAgent
from ag_core.agents.researcher import ResearcherAgent
from ag_core.agents.grok_researcher import GrokResearcherAgent
from ag_core.agents.codex_reviewer import CodexReviewerAgent
from ag_core.agents.tester import TesterAgent
from ag_core.agents.security_agent import SecurityAgent
from ag_core.agents.devops_agent import DevOpsAgent
from ag_core.eval.judge import JUDGE_SYSTEM_PROMPT, default_judge
from orchestrator import parse_debate_verdict, is_debate_approved, run_pipeline


# ===========================================================================
# 1. Few-Shot Exemplar Syntax & Pydantic Schema Conformance
# ===========================================================================

def test_architect_few_shot_conforms_to_design_plan():
    """Verify that ARCHITECT_FEW_SHOT parses cleanly into the DesignPlan schema."""
    clean_json = ARCHITECT_FEW_SHOT.strip()
    if clean_json.startswith("```json"):
        clean_json = clean_json[7:]
    if clean_json.endswith("```"):
        clean_json = clean_json[:-3]
    data = json.loads(clean_json.strip())
    plan = DesignPlan.model_validate(data)
    assert plan.project_name == "rate_limiter_service"
    assert len(plan.files) == 7
    for f in plan.files:
        assert f.path
        assert f.specification


def test_security_few_shots_conform_to_security_report():
    """Verify that clean and vulnerable security few-shots conform to SecurityReport."""
    # Clean
    clean_json = SECURITY_FEW_SHOT_CLEAN.strip()
    if clean_json.startswith("```json"):
        clean_json = clean_json[7:]
    if clean_json.endswith("```"):
        clean_json = clean_json[:-3]
    clean_rep = SecurityReport.model_validate(json.loads(clean_json.strip()))
    assert clean_rep.blocking is False
    assert clean_rep.score >= 9.0
    assert len(clean_rep.findings) == 0

    # Vulnerable
    vuln_json = SECURITY_FEW_SHOT_VULNERABLE.strip()
    if vuln_json.startswith("```json"):
        vuln_json = vuln_json[7:]
    if vuln_json.endswith("```"):
        vuln_json = vuln_json[:-3]
    vuln_rep = SecurityReport.model_validate(json.loads(vuln_json.strip()))
    assert vuln_rep.blocking is True
    assert vuln_rep.score <= 5.0
    assert len(vuln_rep.findings) == 2
    assert len(vuln_rep.blocking_findings) == 2


def test_critic_few_shots_conform_to_debate_verdict():
    """Verify that critique and approval few-shots conform to DebateVerdict."""
    # Critique
    crit_json = CRITIC_FEW_SHOT_CRITIQUE.strip()
    if crit_json.startswith("```json"):
        crit_json = crit_json[7:]
    if crit_json.endswith("```"):
        crit_json = crit_json[:-3]
    crit_verdict = DebateVerdict.model_validate(json.loads(crit_json.strip()))
    assert crit_verdict.approved is False
    assert crit_verdict.score == 3.2
    assert len(crit_verdict.flaws) == 2
    assert crit_verdict.is_approved is False

    # Approval
    appr_json = CRITIC_FEW_SHOT_APPROVAL.strip()
    if appr_json.startswith("```json"):
        appr_json = appr_json[7:]
    if appr_json.endswith("```"):
        appr_json = appr_json[:-3]
    appr_verdict = DebateVerdict.model_validate(json.loads(appr_json.strip()))
    assert appr_verdict.approved is True
    assert appr_verdict.score == 4.8
    assert len(appr_verdict.flaws) == 0
    assert appr_verdict.is_approved is True


def test_coder_and_tester_few_shots_valid_python_ast():
    """Verify that code exemplars in CODER and TESTER few-shots are syntactically valid Python AST."""
    def extract_py(block: str) -> str:
        lines = block.split("\n")
        code_lines = []
        in_code = False
        for line in lines:
            if line.startswith("```python"):
                in_code = True
                continue
            if in_code and line.startswith("```"):
                in_code = False
                continue
            if in_code:
                code_lines.append(line)
        return "\n".join(code_lines)

    coder_code = extract_py(CODER_FEW_SHOT)
    assert len(coder_code) > 0
    ast.parse(coder_code)

    tester_code = extract_py(TESTER_FEW_SHOT)
    assert len(tester_code) > 0
    ast.parse(tester_code)


def test_devops_few_shot_valid_yaml_and_dockerfile():
    """Verify that devops few shot artifacts contain valid YAML workflows and dockerfile markers."""
    assert "# filepath: Dockerfile" in DEVOPS_FEW_SHOT
    assert "# filepath: docker-compose.yml" in DEVOPS_FEW_SHOT
    assert "# filepath: .github/workflows/ci.yml" in DEVOPS_FEW_SHOT

    # Extract yaml blocks and validate
    lines = DEVOPS_FEW_SHOT.split("\n")
    yaml_blocks = []
    curr = []
    in_yaml = False
    for line in lines:
        if line.startswith("```yaml"):
            in_yaml = True
            curr = []
            continue
        if in_yaml and line.startswith("```"):
            in_yaml = False
            yaml_blocks.append("\n".join(curr))
            continue
        if in_yaml:
            curr.append(line)

    assert len(yaml_blocks) >= 2
    for block in yaml_blocks:
        # Strip # filepath comment line
        clean_block = "\n".join([l for l in block.splitlines() if not l.strip().startswith("#")])
        parsed = yaml.safe_load(clean_block)
        assert isinstance(parsed, dict)


# ===========================================================================
# 2. Template Rendering & Format String Escaping Stress Test
# ===========================================================================

def test_prompt_template_formatting_with_adversarial_braces_and_specials():
    """Verify that format templates never raise KeyError or ValueError when inputs contain braces, regexes, and JSON."""
    adversarial_inputs = [
        "",
        "Normal string without braces",
        "{'key': 'value', 'nested': {'inner': [1, 2, 3]}}",
        "Regex patterns: r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\\.[a-zA-Z0-9-.]+$'",
        "Python code with f-strings: f'{user.id}: {user.name}' and {dict(a=1)}",
        "Curly braces in logs: KeyError: 'missing_key_in_{dict}' at line {42}",
        "Unbalanced braces: { { } }}} {{{{",
        "Unicode and emojis: 🚀 \u0000 \n\r\t 特殊字符",
    ]

    for adv in adversarial_inputs:
        # CRITIC_DEBATE_PROMPT
        critic_p = CRITIC_DEBATE_PROMPT.format(draft_plan=adv, context=adv)
        assert adv in critic_p
        assert "CriticReviewer" in critic_p

        # ARCHITECT_REFINE_PROMPT
        refine_p = ARCHITECT_REFINE_PROMPT.format(draft_plan=adv, criticism=adv, context=adv)
        assert adv in refine_p
        assert "Refine your draft architecture plan" in refine_p

        # DEBATE_SYNTHESIS_PROMPT
        synth_p = DEBATE_SYNTHESIS_PROMPT.format(draft_plan=adv, criticism=adv, context=adv)
        assert adv in synth_p
        assert "consensus synthesis" in synth_p

        # CODEX_RETRY_PROMPT
        codex_p = CODEX_RETRY_PROMPT.format(
            file_path="src/main.py",
            exit_code=1,
            error_logs=adv,
            security_report=adv,
        )
        assert "src/main.py" in codex_p
        assert adv in codex_p

        # TESTER_RETRY_PROMPT
        tester_p = TESTER_RETRY_PROMPT.format(
            file_path="tests/test_app.py",
            exit_code=2,
            error_logs=adv,
        )
        assert "tests/test_app.py" in tester_p
        assert adv in tester_p


# ===========================================================================
# 3. Debate Verdict Parsing & Approval Matrix (Boundary & Edge Cases)
# ===========================================================================

@pytest.mark.parametrize(
    "payload,expected_approved",
    [
        # Pure JSON structured verdict - explicitly approved
        ('```json\n{"approved": true, "score": 5.0, "flaws": []}\n```', True),
        ('```json\n{"approved": true, "score": 2.0, "flaws": ["needs work"]}\n```', True),
        # Pure JSON structured verdict - implicit approval via score >= 4.0 and no flaws
        ('```json\n{"approved": false, "score": 4.5, "flaws": []}\n```', True),
        ('```json\n{"approved": false, "score": 4.0, "flaws": []}\n```', True),
        ('```json\n{"score": 4.2, "flaws": []}\n```', True),
        ('```json\n{"score": 4.0}\n```', True),  # flaws defaults to []
        # Pure JSON structured verdict - rejected cases
        ('```json\n{"approved": false, "score": 3.99, "flaws": []}\n```', False),
        ('```json\n{"approved": false, "score": 4.0, "flaws": ["gap in auth"]}\n```', False),
        ('```json\n{"approved": false, "score": 4.8, "flaws": ["critical bug"]}\n```', False),
        ('```json\n{"score": 3.5, "flaws": []}\n```', False),
        ('```json\n{"score": 1.0, "flaws": ["broken"]}\n```', False),
        # Legacy string format (no JSON object)
        ("The plan looks solid and complete. [APPROVED]", True),
        ("LGTM! [APPROVED] proceed to implementation.", True),
        ("The architecture has several flaws that must be addressed.", False),
        ("", False),
        (None, False),
        # Structured verdict takes precedence over legacy [APPROVED] marker in prose
        (
            '```json\n{"approved": false, "score": 3.0, "flaws": ["missing caching"]}\n```\n'
            'Note: do NOT consider this [APPROVED] until caching is added.',
            False
        ),
        # JSON without code fences
        ('{"approved": true, "score": 4.5, "flaws": []}', True),
        ('{"approved": false, "score": 3.2, "flaws": ["bug"]}', False),
        # Nested criteria scores
        (
            '```json\n{"approved": false, "score": 4.2, "criteria_scores": {"completeness": 5.0, "security": 4.0}, "flaws": []}\n```',
            True
        ),
    ]
)
def test_debate_approval_matrix(payload, expected_approved):
    """Stress-test is_debate_approved across all boundary and formatting combinations."""
    assert is_debate_approved(payload) is expected_approved


def test_parse_debate_verdict_malformed_resilience():
    """Verify that parse_debate_verdict gracefully handles broken or incomplete json."""
    # Truncated json
    assert parse_debate_verdict("```json\n{\"approved\": true, ") is None
    # Array instead of object
    assert parse_debate_verdict("```json\n[1, 2, 3]\n```") is None
    # Completely empty dict without matching keys
    v = parse_debate_verdict("```json\n{\"unknown_key\": 123}\n```")
    assert v is None
    # Dict with invalid types
    v = parse_debate_verdict("```json\n{\"score\": \"invalid_score\"}\n```")
    assert v is None


# ===========================================================================
# 4. Multi-Round Debate Orchestration Scenarios (Empirical Mock Execution)
# ===========================================================================

@pytest.mark.asyncio
async def test_mcp_run_debate_early_approval():
    """Test MCP _run_debate with immediate approval on round 1."""
    from mcp_server import _run_debate

    critique_resp = '```json\n{"approved": true, "score": 4.9, "flaws": []}\n```'

    with mock.patch("mcp_server.execute_agent", new_callable=mock.AsyncMock) as mock_exec:
        mock_exec.side_effect = [critique_resp]

        result_raw = await _run_debate(design="Initial Plan", prompt="Build rate limiter", rounds=3)
        result = json.loads(result_raw)

        assert result["approved"] is True
        assert len(result["rounds"]) == 1
        assert result["rounds"][0]["approved"] is True
        # Ensure Claude refine was never called since round 1 approved
        assert mock_exec.call_count == 1
        mock_exec.assert_called_once_with("research", mock.ANY, {})


@pytest.mark.asyncio
async def test_mcp_run_debate_refinement_then_approval():
    """Test MCP _run_debate where round 1 critiques, round 2 approves."""
    from mcp_server import _run_debate

    critique_1 = '```json\n{"approved": false, "score": 3.0, "flaws": ["Missing Redis fallback"]}\n```'
    refined_plan_1 = '{"project_name": "limiter", "files": [{"path": "src/limiter.py", "specification": "With fallback"}]}'
    critique_2 = '```json\n{"approved": true, "score": 4.8, "flaws": []}\n```'

    with mock.patch("mcp_server.execute_agent", new_callable=mock.AsyncMock) as mock_exec:
        mock_exec.side_effect = [critique_1, refined_plan_1, critique_2]

        result_raw = await _run_debate(design="Initial Plan", prompt="Build rate limiter", rounds=3)
        result = json.loads(result_raw)

        assert result["approved"] is True
        assert len(result["rounds"]) == 2
        assert result["rounds"][0]["approved"] is False
        assert result["rounds"][1]["approved"] is True
        assert mock_exec.call_count == 3
        # Round 1 refine used ARCHITECT_REFINE_PROMPT
        round1_refine_prompt = mock_exec.call_args_list[1][0][1]
        assert "Refine your draft architecture plan based on the constructive criticism" in round1_refine_prompt


@pytest.mark.asyncio
async def test_mcp_run_debate_exhaustion_triggers_consensus_synthesis():
    """Test MCP _run_debate reaching round limit (e.g. 2 rounds) triggers DEBATE_SYNTHESIS_PROMPT."""
    from mcp_server import _run_debate

    critique_1 = '```json\n{"approved": false, "score": 2.5, "flaws": ["Flaw 1"]}\n```'
    refined_plan_1 = '{"project_name": "limiter", "files": []}'
    critique_2 = '```json\n{"approved": false, "score": 3.5, "flaws": ["Flaw 2"]}\n```'
    synthesized_plan = '{"project_name": "limiter_synthesized", "files": []}'

    with mock.patch("mcp_server.execute_agent", new_callable=mock.AsyncMock) as mock_exec:
        mock_exec.side_effect = [critique_1, refined_plan_1, critique_2, synthesized_plan]

        result_raw = await _run_debate(design="Initial Plan", prompt="Build rate limiter", rounds=2)
        result = json.loads(result_raw)

        assert result["approved"] is False
        assert len(result["rounds"]) == 2
        assert result["rounds"][1]["synthesized"] is True
        assert mock_exec.call_count == 4
        # Round 2 refine used DEBATE_SYNTHESIS_PROMPT
        round2_refine_prompt = mock_exec.call_args_list[3][0][1]
        assert "consensus synthesis" in round2_refine_prompt


# ===========================================================================
# 5. Agent Classes Instantiation & Prompt Composition across all 7 Roles
# ===========================================================================

class DummyProvider:
    def __init__(self, model_name="mock-model"):
        self.model_name = model_name
        self.last_system_prompt = None

    async def send_prompt(self, prompt: str, system: str = ""):
        self.last_system_prompt = system
        return {"content": "MOCK_RESPONSE", "usage": {"prompt_tokens": 10, "completion_tokens": 20}}


@pytest.mark.asyncio
async def test_all_7_agent_classes_execution_and_prompts():
    """Verify error-free instantiation, slash routing, and execution across all 7 agent classes."""
    provider = DummyProvider()

    # 1. ClaudeArchitectAgent
    arch = ClaudeArchitectAgent(provider=provider, use_memory=False, output_file="None")
    assert "DesignPlan JSON schema" in arch.SYSTEM_PROMPT
    assert "rate_limiter_service" in arch.SYSTEM_PROMPT
    arch_res = await arch.run(prompt="/plan Build distributed cache")
    assert arch_res == "MOCK_RESPONSE"
    assert "DesignPlan JSON schema" in provider.last_system_prompt

    # 2. ResearcherAgent / GrokResearcherAgent
    res_agent = ResearcherAgent(provider=provider, use_memory=False, output_file="None")
    assert "Exemplar Technical Research Brief" in res_agent.SYSTEM_PROMPT
    assert AGENT_CORE_RULES in res_agent.SYSTEM_PROMPT
    r_res = await res_agent.run(prompt="/research Redis cluster")
    assert r_res == "MOCK_RESPONSE"
    assert "Exemplar Technical Research Brief" in provider.last_system_prompt

    grok_agent = GrokResearcherAgent(provider=provider, use_memory=False, output_file="None")
    g_res = await grok_agent.run(prompt="/fact-check Latency numbers")
    assert g_res == "MOCK_RESPONSE"

    # 3. CodexReviewerAgent (generation mode & review mode)
    codex_agent = CodexReviewerAgent(provider=provider, use_memory=False, output_file="None")
    # /code generation mode
    code_res = await codex_agent.run(prompt="/code Write limiter in src/limiter.py")
    assert code_res == "MOCK_RESPONSE"
    assert CODER_PROMPT in provider.last_system_prompt

    # review mode
    rev_res = await codex_agent.run(prompt="/security Check sql injection in app.py")
    assert "MOCK_RESPONSE" in rev_res

    # 4. TesterAgent
    assert "Exemplar Test Suite" in TESTER_PROMPT
    assert AGENT_CORE_RULES in TESTER_PROMPT
    tester_agent = TesterAgent(provider=provider, use_memory=False, output_file="None")
    test_res = await tester_agent.run(prompt="/unit-test Generate tests for limiter")
    assert "MOCK_RESPONSE" in test_res
    assert "Exemplar Test Suite" in provider.last_system_prompt

    # 5. SecurityAgent
    sec_agent = SecurityAgent(provider=provider, use_memory=False, output_file="None")
    assert "SecurityReport schema" in sec_agent.SYSTEM_PROMPT
    assert "Code Injection (CWE-95)" in sec_agent.SYSTEM_PROMPT
    sec_res = await sec_agent.run(prompt="/audit Scan src/ for secrets")
    assert sec_res == "MOCK_RESPONSE"
    assert "SecurityReport schema" in provider.last_system_prompt

    # 6. DevOpsAgent
    devops_agent = DevOpsAgent(provider=provider, use_memory=False, output_file="None")
    assert "# filepath: Dockerfile" in devops_agent.SYSTEM_PROMPT
    assert AGENT_CORE_RULES in devops_agent.SYSTEM_PROMPT
    devops_res = await devops_agent.run(prompt="/deploy Generate Docker and CI")
    assert devops_res == "MOCK_RESPONSE"
    assert "# filepath: Dockerfile" in provider.last_system_prompt

    # 7. Judge (ag_core.eval.judge)
    assert "Score Calibration Anchors" in JUDGE_SYSTEM_PROMPT
    assert "Score 1 (Fails Goal)" in JUDGE_SYSTEM_PROMPT
    assert "Score 5 (Production-Ready)" in JUDGE_SYSTEM_PROMPT
    with mock.patch("ag_core.provider_factory.make_provider") as mock_mp:
        mock_provider = mock.AsyncMock()
        mock_provider.send_prompt.return_value = {"content": '{"score": 5, "explanation": "Excellent"}'}
        mock_mp.return_value = mock_provider
        judge_callable = default_judge()
        judge_out = await judge_callable("Evaluate this implementation...")
        assert '{"score": 5' in judge_out
        assert mock_provider.send_prompt.call_args[1]["system"] == JUDGE_SYSTEM_PROMPT


# ===========================================================================
# 6. Orchestrator Pipeline Debate & Consensus Synthesis Integration Tests
# ===========================================================================

@pytest.fixture
def temp_workspace(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    return tmp_path


async def _mock_subprocess_exec(*args, **kwargs):
    proc = mock.MagicMock()

    async def mock_communicate():
        app_file = os.path.join("projects", "test_debate_app", "src", "app.py")
        os.makedirs(os.path.dirname(app_file), exist_ok=True)
        with open(app_file, "w", encoding="utf-8") as f:
            f.write("def main(): pass")
        return (b"mock output", b"")

    proc.communicate = mock_communicate
    proc.returncode = 0
    return proc


@pytest.mark.asyncio
@mock.patch("orchestrator.call_api", new_callable=mock.MagicMock)
@mock.patch("asyncio.create_subprocess_exec", new_callable=mock.MagicMock)
async def test_orchestrator_debate_structured_verdict_approval(mock_exec, mock_call_api, temp_workspace):
    """Test orchestrator debate loop with structured DebateVerdict approval on round 1."""
    responses = [
        "Researcher findings",
        '{"project_name": "test_debate_app", "files": [{"path": "src/app.py", "specification": "Main entry"}]}',
        '```json\n{"approved": true, "score": 4.9, "flaws": []}\n```',  # Structured approval
        "```python\ndef main(): pass\n```",  # Codex code
        "```python\ndef test_main(): pass\n```",  # Tester
        '```json\n{"blocking": false, "score": 10.0, "findings": []}\n```',  # Security
        "```dockerfile\n# filepath: Dockerfile\nFROM alpine\n```",  # DevOps
    ]

    call_idx = 0
    calls_recorded = []

    async def mock_call_api_impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        nonlocal call_idx
        calls_recorded.append((url, prompt))
        resp = responses[call_idx]
        call_idx += 1
        return resp

    mock_call_api.side_effect = mock_call_api_impl
    mock_exec.side_effect = _mock_subprocess_exec

    await run_pipeline(
        prompt="Test debate app",
        workspace=str(temp_workspace),
        max_debate_rounds=2,
    )

    # Verify that critic round 1 was called and early exit occurred
    critic_calls = [c for c in calls_recorded if "CriticReviewer" in c[1]]
    assert len(critic_calls) == 1
    # Verify refine was NOT called
    refine_calls = [
        c for c in calls_recorded
        if "Refine your draft architecture plan" in c[1] or "consensus synthesis" in c[1]
    ]
    assert len(refine_calls) == 0


@pytest.mark.asyncio
@mock.patch("orchestrator.call_api", new_callable=mock.MagicMock)
@mock.patch("asyncio.create_subprocess_exec", new_callable=mock.MagicMock)
async def test_orchestrator_debate_exhaustion_triggers_consensus_synthesis(mock_exec, mock_call_api, temp_workspace):
    """Test orchestrator debate loop reaching round limit triggers DEBATE_SYNTHESIS_PROMPT."""
    responses = [
        "Researcher findings",  # 1. Research
        '{"project_name": "test_debate_app", "files": [{"path": "src/app.py", "specification": "Main entry"}]}',  # 2. Initial Claude
        '```json\n{"approved": false, "score": 2.5, "flaws": ["Flaw A"]}\n```',  # 3. Critic round 1
        '{"project_name": "test_debate_app", "files": [{"path": "src/app.py", "specification": "Refined v1"}]}',  # 4. Claude refine 1
        '```json\n{"approved": false, "score": 3.5, "flaws": ["Flaw B"]}\n```',  # 5. Critic round 2
        '{"project_name": "test_debate_app", "files": [{"path": "src/app.py", "specification": "Synthesized final"}]}',  # 6. Claude synthesis 2
        "```python\ndef main(): pass\n```",  # 7. Codex
        "```python\ndef test_main(): pass\n```",  # 8. Tester
        '```json\n{"blocking": false, "score": 10.0, "findings": []}\n```',  # 9. Security
        "```dockerfile\n# filepath: Dockerfile\nFROM alpine\n```",  # 10. DevOps
    ]

    call_idx = 0
    calls_recorded = []

    async def mock_call_api_impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        nonlocal call_idx
        calls_recorded.append((url, prompt))
        resp = responses[call_idx]
        call_idx += 1
        return resp

    mock_call_api.side_effect = mock_call_api_impl
    mock_exec.side_effect = _mock_subprocess_exec

    await run_pipeline(
        prompt="Test debate app",
        workspace=str(temp_workspace),
        max_debate_rounds=2,
    )

    # Verify that round 1 refine used ARCHITECT_REFINE_PROMPT
    refine_1_call = calls_recorded[3]
    assert "Refine your draft architecture plan based on the constructive criticism" in refine_1_call[1]

    # Verify that round 2 refine used DEBATE_SYNTHESIS_PROMPT
    synthesis_call = calls_recorded[5]
    assert "consensus synthesis" in synthesis_call[1]
    assert "reconcile all feedback and critique" in synthesis_call[1].lower()


@pytest.mark.asyncio
@mock.patch("orchestrator.call_api", new_callable=mock.MagicMock)
@mock.patch("asyncio.create_subprocess_exec", new_callable=mock.MagicMock)
async def test_orchestrator_debate_degraded_mode_resilience(mock_exec, mock_call_api, temp_workspace, monkeypatch):
    """Test that in degraded mode (GENIUS_DEGRADED_MODE=1), a debate failure keeps current design and proceeds."""
    monkeypatch.setenv("GENIUS_DEGRADED_MODE", "1")

    responses = [
        "Researcher findings",  # 1. Research
        '{"project_name": "test_debate_app", "files": [{"path": "src/app.py", "specification": "Main entry"}]}',  # 2. Initial Claude
        # Critic round 1 will raise Exception
        "```python\ndef main(): pass\n```",  # Codex
        "```python\ndef test_main(): pass\n```",  # Tester
        '```json\n{"blocking": false, "score": 10.0, "findings": []}\n```',  # Security
        "```dockerfile\n# filepath: Dockerfile\nFROM alpine\n```",  # DevOps
    ]

    call_idx = 0

    async def mock_call_api_impl(url, api_key, prompt, context=None, client=None, poll_timeout=60.0):
        nonlocal call_idx
        if call_idx == 2:
            call_idx += 1
            raise RuntimeError("Simulated Critic Network Timeout in Degraded Mode")
        resp = responses[call_idx]
        call_idx += 1
        return resp

    mock_call_api.side_effect = mock_call_api_impl
    mock_exec.side_effect = _mock_subprocess_exec

    # Must complete successfully despite critic failure in degraded mode
    await run_pipeline(
        prompt="Test debate app",
        workspace=str(temp_workspace),
        max_debate_rounds=2,
    )
