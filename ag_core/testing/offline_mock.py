"""Offline Mock Provider & Stream Emulation Infrastructure.

This module provides deterministic, 100% offline mock implementations of all
LLM providers and CLI streams for Genius Antigravity 2.0.
"""

import asyncio
import json
import logging
import re
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple, Union
from pydantic import BaseModel, Field

from ag_core.interfaces.base_provider import BaseProvider, ProviderResponse, TokenUsage
from ag_core.models import (
    DebateVerdict,
    DesignFile,
    DesignPlan,
    SecurityFinding,
    SecurityReport,
)
from ag_core.utils.cli_runner import CLITimeoutError

logger = logging.getLogger("ag_core.testing")


# ---------------------------------------------------------------------------
# CLI Stream Emulator
# ---------------------------------------------------------------------------


class CLIStreamEmulator:
    """Emulates stdout/stderr byte streams for vendor CLIs (Codex, Claude, Grok, Agy, NLM)."""

    @staticmethod
    def codex_jsonl(
        text_content: str,
        prompt_tokens: int = 100,
        completion_tokens: int = 50,
        error_message: Optional[str] = None,
        thread_id: str = "mock-thread-001",
    ) -> bytes:
        """Generates standard OpenAI Codex JSONL event stream."""
        lines = [
            json.dumps({"event": "thread.started", "thread_id": thread_id}),
            json.dumps({"event": "turn.started"}),
        ]
        if error_message:
            lines.append(
                json.dumps({
                    "event": "turn.failed",
                    "error": {"message": error_message},
                })
            )
            lines.append(
                json.dumps({"event": "error", "message": error_message})
            )
        else:
            lines.append(
                json.dumps({
                    "event": "agent_message",
                    "item": {"type": "agent_message", "text": text_content},
                })
            )
            total = prompt_tokens + completion_tokens
            lines.append(
                json.dumps({
                    "event": "turn.completed",
                    "turn.completed": {
                        "usage": {
                            "input_tokens": prompt_tokens,
                            "output_tokens": completion_tokens,
                            "total_tokens": total,
                        }
                    },
                })
            )
        return ("\n".join(lines) + "\n").encode("utf-8")

    @staticmethod
    def claude_json(
        text_content: str,
        prompt_tokens: int = 100,
        completion_tokens: int = 50,
        is_error: bool = False,
        error_msg: Optional[str] = None,
    ) -> bytes:
        """Generates standard Claude Code CLI JSON envelope."""
        envelope: Dict[str, Any] = {
            "result": text_content if not is_error else (error_msg or text_content),
            "is_error": is_error,
            "usage": {
                "input_tokens": prompt_tokens,
                "output_tokens": completion_tokens,
            },
        }
        if error_msg:
            envelope["error"] = error_msg
        return json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def grok_json(
        text_content: str,
        session_id: str = "mock-sess-1",
        prompt_tokens: int = 80,
        completion_tokens: int = 40,
        is_error: bool = False,
        error_msg: Optional[str] = None,
    ) -> bytes:
        """Generates standard xAI Grok Build CLI JSON envelope."""
        if is_error or error_msg:
            envelope: Dict[str, Any] = {
                "type": "error",
                "message": error_msg or "Grok execution error",
                "is_error": True,
            }
        else:
            envelope = {
                "text": text_content,
                "stopReason": "EndTurn",
                "sessionId": session_id,
                "usage": {
                    "input_tokens": prompt_tokens,
                    "output_tokens": completion_tokens,
                },
            }
        return json.dumps(envelope, ensure_ascii=False).encode("utf-8")

    @staticmethod
    def agy_plain_text(text_content: str) -> bytes:
        """Generates plain text output for Antigravity 2.0 / Gemini CLI."""
        return (text_content.rstrip() + "\n").encode("utf-8")

    @staticmethod
    def nlm_json(
        answer_text: str,
        notebook_id: str = "mock-nb-01",
        citations: Optional[List[Dict[str, Any]]] = None,
        references: Optional[List[str]] = None,
    ) -> bytes:
        """Generates NotebookLM query JSON output."""
        data = {
            "answer": answer_text,
            "notebook_id": notebook_id,
            "conversation_id": f"conv-{uuid.uuid4().hex[:8]}",
            "citations": citations or [{"source": "doc1", "text": "Evidence snippet from notebook."}],
            "references": references or ["architecture_spec.pdf"],
        }
        return json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8")

    @staticmethod
    def nlm_notebook_list(notebooks: Optional[List[Dict[str, Any]]] = None) -> bytes:
        """Generates NotebookLM notebook list JSON output."""
        items = notebooks or [
            {"id": "mock-nb-01", "title": "Genius Knowledge Base", "source_count": 5},
            {"id": "mock-nb-02", "title": "System Architecture", "source_count": 2},
        ]
        return json.dumps(items, ensure_ascii=False, indent=2).encode("utf-8")

    @staticmethod
    def nlm_notebook_create(notebook_id: str = "mock-nb-new", title: str = "New Notebook") -> bytes:
        """Generates NotebookLM notebook create JSON output."""
        return json.dumps({"id": notebook_id, "title": title}, ensure_ascii=False).encode("utf-8")

    @classmethod
    def emulate(cls, flavor: str, text_content: str, **kwargs: Any) -> bytes:
        """Dispatches to the appropriate encoder according to backend flavor."""
        flavor = (flavor or "").lower()
        if "codex" in flavor or "openai" in flavor:
            return cls.codex_jsonl(text_content, **kwargs)
        elif "claude" in flavor or "anthropic" in flavor:
            return cls.claude_json(text_content, **kwargs)
        elif "grok" in flavor:
            return cls.grok_json(text_content, **kwargs)
        elif "agy" in flavor or "gemini" in flavor:
            return cls.agy_plain_text(text_content)
        elif "nlm" in flavor or "notebooklm" in flavor:
            return cls.nlm_json(text_content, **kwargs)
        else:
            return cls.codex_jsonl(text_content, **kwargs)


# ---------------------------------------------------------------------------
# Scenario & Fault Injection Rules
# ---------------------------------------------------------------------------


class FaultInjectionRule(BaseModel):
    """Configuration for injecting simulated faults into mock providers."""

    fault_type: str = Field(
        ...,
        description="Type of fault: 'rate_limit' (429), 'auth_error' (401/403), 'timeout', 'crash', 'corrupt_output'",
    )
    attempts: int = Field(
        default=1,
        description="Number of consecutive attempts to trigger this fault before recovering",
    )
    pattern: str = Field(
        default=".*",
        description="Regex pattern to match against the prompt / task",
    )
    role: Optional[str] = Field(
        default=None,
        description="Specific agent role to target (e.g. 'tester', 'security')",
    )
    retry_after: float = Field(
        default=0.1,
        description="Retry-After header value in seconds for 429 rate limits",
    )


class MockScenarioRule(BaseModel):
    """Rule matching prompt criteria and supplying deterministic mock responses or faults."""

    pattern: str = Field(..., description="Regex pattern to match in prompt or system prompt")
    response: Union[str, Dict[str, Any], Any] = Field(
        default="", description="Response string, dict, or callable generator"
    )
    status_code: int = 200
    delay_seconds: float = 0.0
    token_usage: TokenUsage = Field(
        default_factory=lambda: TokenUsage(prompt_tokens=50, completion_tokens=100, total_tokens=150)
    )
    fail_attempts: int = 0
    error_type: Optional[str] = None
    retry_after: float = 0.1
    role: Optional[str] = None

    def matches(self, prompt: str, system: Optional[str] = None, role: Optional[str] = None) -> bool:
        """Determines if the rule matches the given prompt, system prompt, or role."""
        if self.role and role:
            if self.role.lower() != role.lower():
                return False
        combined = f"{system or ''}\n\n{prompt}"
        try:
            return bool(re.search(self.pattern, combined, re.DOTALL | re.IGNORECASE))
        except re.error:
            return self.pattern.lower() in combined.lower()


# ---------------------------------------------------------------------------
# Default Role Response Generators
# ---------------------------------------------------------------------------


def generate_default_researcher_response(prompt: str) -> str:
    """Generates structured technical research brief."""
    return (
        "# Technical Discovery Brief: System Implementation\n\n"
        "## Requirements\n"
        "- Implement robust, production-grade components specified in the user request.\n"
        "- Ensure clean interface boundaries and modular separation of concerns.\n"
        "- Provide comprehensive automated test coverage with pytest.\n\n"
        "## Constraints\n"
        "- Python 3.11+ using standard async and pydantic models.\n"
        "- Safe local execution with proper error handling and logging.\n"
        "- Zero external API credentials required for offline execution.\n\n"
        "## Dependencies\n"
        "- `pydantic`\n"
        "- `pytest`\n"
        "- `pytest-asyncio`\n\n"
        "## Risks\n"
        "- Concurrency race conditions in multi-threaded workflows -> Mitigate with asyncio locks.\n"
        "- Subprocess hangs on external command calls -> Mitigate with communicate_with_timeout.\n\n"
        "## Open Questions\n"
        "- Should rate limits be enforced in-memory or persisted to disk?\n"
    )


def generate_default_architect_response(prompt: str) -> str:
    """Generates valid DesignPlan JSON block."""
    plan = DesignPlan(
        project_name="genius_app",
        description="Production-ready multi-tier system with modular services and complete test suite.",
        architecture="Three-tier architecture comprising data models, service layer, and CLI entry point.",
        files=[
            DesignFile(
                path="src/models.py",
                specification=(
                    "Define core data models using Pydantic v2. Include AppConfig, "
                    "TaskRequest, and TaskResult models with complete field validations and type annotations."
                ),
            ),
            DesignFile(
                path="src/service.py",
                specification=(
                    "Implement CoreService class managing task execution pipeline. "
                    "Include execute_task(req: TaskRequest) -> TaskResult with robust error handling and logging."
                ),
            ),
            DesignFile(
                path="tests/test_service.py",
                specification=(
                    "Implement pytest test suite covering CoreService initialization, "
                    "successful task execution, and exception handling on invalid inputs."
                ),
            ),
        ],
        dependencies=["pydantic", "pytest", "pytest-asyncio"],
    )
    return f"```json\n{plan.model_dump_json(indent=2)}\n```"


def generate_default_coder_response(prompt: str) -> str:
    """Generates valid Python code based on target file in prompt."""
    if "test_" in prompt.lower() or "tests/" in prompt.lower():
        return (
            "```python\n"
            "import pytest\n"
            "from unittest.mock import AsyncMock\n\n"
            "@pytest.mark.asyncio\n"
            "async def test_service_execution():\n"
            "    mock_engine = AsyncMock()\n"
            "    mock_engine.run.return_value = {'status': 'success', 'data': 42}\n"
            "    result = await mock_engine.run()\n"
            "    assert result['status'] == 'success'\n"
            "    assert result['data'] == 42\n"
            "```"
        )
    elif "model" in prompt.lower():
        return (
            "```python\n"
            "from typing import Optional, Dict, Any\n"
            "from pydantic import BaseModel, Field\n\n"
            "class TaskRequest(BaseModel):\n"
            "    task_id: str = Field(..., description='Unique task identifier')\n"
            "    payload: Dict[str, Any] = Field(default_factory=dict)\n\n"
            "class TaskResult(BaseModel):\n"
            "    task_id: str\n"
            "    status: str = 'completed'\n"
            "    result: Dict[str, Any] = Field(default_factory=dict)\n"
            "```"
        )
    else:
        return (
            "```python\n"
            "import asyncio\n"
            "import logging\n"
            "from typing import Dict, Any\n\n"
            "logger = logging.getLogger(__name__)\n\n"
            "class CoreService:\n"
            "    def __init__(self, name: str = 'CoreService') -> None:\n"
            "        self.name = name\n"
            "        self._running = True\n\n"
            "    async def execute_task(self, task_id: str, payload: Dict[str, Any]) -> Dict[str, Any]:\n"
            "        logger.info(f'Executing task {task_id}')\n"
            "        await asyncio.sleep(0.01)\n"
            "        return {'task_id': task_id, 'status': 'completed', 'data': payload}\n"
            "```"
        )


def generate_default_reviewer_response(prompt: str) -> str:
    """Generates code review report."""
    return (
        "# Code Review & Quality Assessment\n\n"
        "## Summary\n"
        "The codebase follows clean software engineering principles. Proper async patterns and type hints are used.\n\n"
        "## Architecture & Design\n"
        "- Well-isolated components with single responsibility.\n"
        "- Clean dependency boundaries.\n\n"
        "## Recommendations\n"
        "- Ensure full test coverage on edge cases.\n"
    )


def generate_default_tester_response(prompt: str) -> str:
    """Generates runnable pytest test suite."""
    return (
        "```python\n"
        "import pytest\n"
        "from unittest.mock import AsyncMock, patch\n\n"
        "@pytest.mark.asyncio\n"
        "async def test_core_functionality():\n"
        "    handler = AsyncMock()\n"
        "    handler.process = AsyncMock(return_value=True)\n"
        "    res = await handler.process()\n"
        "    assert res is True\n\n"
        "@pytest.mark.asyncio\n"
        "async def test_boundary_conditions():\n"
        "    with pytest.raises(ValueError):\n"
        "        raise ValueError('Invalid input parameters')\n"
        "```"
    )


def generate_default_security_response(prompt: str) -> str:
    """Generates SecurityReport JSON block."""
    is_vulnerable = any(
        kw in prompt.lower()
        for kw in ("vulnerability", "hardcoded", "malicious", "sql injection", "secret", "eval(", "exploit")
    )
    if is_vulnerable and "clean" not in prompt.lower():
        report = SecurityReport(
            blocking=True,
            score=3.5,
            summary="Found 1 critical remote code execution vulnerability and 1 high risk secret exposure.",
            findings=[
                SecurityFinding(
                    severity="critical",
                    category="Code Injection (CWE-95)",
                    type="Remote Code Execution",
                    file="src/service.py",
                    line=42,
                    issue="Unsanitized eval() executed on input parameter",
                    fix="Use ast.literal_eval() or safe parsing logic instead of raw eval().",
                ),
                SecurityFinding(
                    severity="high",
                    category="Hardcoded Secret (CWE-798)",
                    type="Secret Exposure",
                    file="src/config.py",
                    line=12,
                    issue="Hardcoded API token in source code",
                    fix="Load secrets exclusively from environment variables.",
                ),
            ],
        )
    else:
        report = SecurityReport(
            blocking=False,
            score=9.8,
            summary="No critical or high severity vulnerabilities detected. Code passes security audit.",
            findings=[],
        )
    return f"```json\n{report.model_dump_json(indent=2)}\n```"


def generate_default_devops_response(prompt: str) -> str:
    """Generates DevOps artifacts with # filepath: markers."""
    return (
        "```dockerfile\n"
        "# filepath: Dockerfile\n"
        "FROM python:3.11-slim\n"
        "WORKDIR /app\n"
        "COPY requirements.txt .\n"
        "RUN pip install --no-cache-dir -r requirements.txt\n"
        "COPY src/ ./src/\n"
        "CMD [\"python\", \"-m\", \"src.main\"]\n"
        "```\n\n"
        "```yaml\n"
        "# filepath: docker-compose.yml\n"
        "version: '3.8'\n"
        "services:\n"
        "  app:\n"
        "    build: .\n"
        "    ports:\n"
        "      - \"8000:8000\"\n"
        "    restart: unless-stopped\n"
        "```\n\n"
        "```yaml\n"
        "# filepath: .github/workflows/ci.yml\n"
        "name: CI Pipeline\n"
        "on: [push, pull_request]\n"
        "jobs:\n"
        "  test:\n"
        "    runs-on: ubuntu-latest\n"
        "    steps:\n"
        "      - uses: actions/checkout@v4\n"
        "      - uses: actions/setup-python@v5\n"
        "        with:\n"
        "          python-version: '3.11'\n"
        "      - run: pip install -r requirements.txt\n"
        "      - run: pytest -v\n"
        "```"
    )


def generate_default_critic_response(prompt: str) -> str:
    """Generates DebateVerdict JSON block or [APPROVED] synthesis."""
    if "[approved]" in prompt.lower() or "final round" in prompt.lower() or "refine" in prompt.lower():
        verdict = DebateVerdict(
            approved=True,
            score=4.8,
            criteria_scores={"completeness": 5.0, "modularity": 4.8, "security": 4.8, "feasibility": 5.0},
            strengths=["Comprehensive file decomposition", "Robust error boundaries"],
            flaws=[],
            feedback="The refined architecture plan is complete, modular, and approved to proceed. [APPROVED]",
            concrete_suggestions=[],
            risks=[],
        )
    else:
        verdict = DebateVerdict(
            approved=True,
            score=4.5,
            criteria_scores={"completeness": 4.5, "modularity": 4.5, "security": 4.5, "feasibility": 4.5},
            strengths=["Clean component separation"],
            flaws=[],
            feedback="Architecture design meets requirements. [APPROVED]",
            concrete_suggestions=[],
            risks=[],
        )
    return f"```json\n{verdict.model_dump_json(indent=2)}\n```"


# ---------------------------------------------------------------------------
# Offline Mock Provider Implementation
# ---------------------------------------------------------------------------


class OfflineMockProvider(BaseProvider):
    """
    100% offline, deterministic mock provider supporting realistic multi-format
    emulation, multi-turn state, tool-calling simulation, and fault injection.
    """

    def __init__(
        self,
        model_name: str = "offline-mock-v1",
        backend_flavor: str = "codex",
        default_role: str = "coder",
        rules: Optional[List[MockScenarioRule]] = None,
        default_response: Optional[str] = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(model_name=model_name, **kwargs)
        self.backend_flavor = backend_flavor.lower()
        self.default_role = default_role.lower()
        self.rules: List[MockScenarioRule] = rules or []
        self.fault_rules: List[FaultInjectionRule] = []
        self.default_response = default_response
        self.call_records: List[Dict[str, Any]] = []
        self.history: List[Dict[str, Any]] = []
        self.sessions: Dict[str, List[Dict[str, Any]]] = {}
        self._attempt_counters: Dict[str, int] = {}
        self._fault_attempt_counters: Dict[str, int] = {}

    def add_rule(self, rule: MockScenarioRule) -> None:
        """Appends a new scenario rule."""
        self.rules.append(rule)

    def inject_fault(
        self,
        fault_type: str,
        attempts: int = 1,
        pattern: str = ".*",
        retry_after: float = 0.1,
        role: Optional[str] = None,
    ) -> None:
        """Injects a simulated fault."""
        rule = FaultInjectionRule(
            fault_type=fault_type,
            attempts=attempts,
            pattern=pattern,
            retry_after=retry_after,
            role=role,
        )
        self.fault_rules.append(rule)

    def clear_faults(self) -> None:
        """Clears all active fault rules and reset counters."""
        self.fault_rules.clear()
        self._fault_attempt_counters.clear()

    def reset(self) -> None:
        """Resets call records, history, counters, and fault rules."""
        self.call_records.clear()
        self.history.clear()
        self.sessions.clear()
        self._attempt_counters.clear()
        self._fault_attempt_counters.clear()
        self.fault_rules.clear()

    @property
    def call_count(self) -> int:
        return len(self.call_records)

    def get_prompts(self) -> List[str]:
        return [record["prompt"] for record in self.call_records]

    def _infer_role(self, prompt: str, system: Optional[str], kwargs: Dict[str, Any]) -> str:
        """Infers the agent role from explicit kwargs, system prompt, or prompt content."""
        explicit_role = kwargs.get("role") or self.extra_params.get("role")
        if explicit_role:
            return str(explicit_role).lower()

        p_head = (prompt[:500] if prompt else "").lower()

        # 1. Match the system prompt (which uniquely identifies specialized agents)
        if system:
            sys_low = system.lower()
            if "criticreviewer" in sys_low or "debateverdict" in sys_low:
                return "critic"
            elif "senior research engineer" in sys_low or "researcher_prompt" in sys_low:
                return "researcher"
            elif "software architect" in sys_low or "designplan" in sys_low:
                return "architect"
            elif "security auditor" in sys_low or "securityreport" in sys_low:
                return "security"
            elif "devops engineer" in sys_low:
                return "devops"
            elif "senior test engineer" in sys_low or "tester_prompt" in sys_low:
                return "tester"
            elif "software engineer implementing exactly one file" in sys_low or "coder_prompt" in sys_low:
                if "code review" in p_head or "review" in p_head:
                    return "reviewer"
                return "coder"

        # 2. Check prompt instruction header (first 500 chars to avoid scanned context noise)
        if "criticreviewer" in p_head or "debateverdict" in p_head:
            return "critic"
        elif "architect" in p_head or "/plan" in p_head or "/design" in p_head or "design plan" in p_head:
            return "architect"
        elif "research" in p_head or "/research" in p_head:
            return "researcher"
        elif "security" in p_head or "/security" in p_head or "/audit" in p_head:
            return "security"
        elif "devops" in p_head or "/deploy" in p_head or "docker" in p_head or "deployment" in p_head:
            return "devops"
        elif "unit test" in p_head or "tester" in p_head or "/unit-test" in p_head or "pytest" in p_head:
            return "tester"
        elif "review" in p_head or "/refactor" in p_head or "refactor" in p_head:
            return "reviewer"
        elif "/code" in p_head or "implement" in p_head or "write code" in p_head:
            return "coder"

        return self.default_role

    async def send_prompt(
        self, prompt: str, system: Optional[str] = None, **kwargs: Any
    ) -> Dict[str, Any]:
        """Processes a prompt deterministically through matching rules or role defaults."""
        async with self.semaphore:
            await self.rate_limiter.acquire()

            role = self._infer_role(prompt, system, kwargs)
            session_id = kwargs.get("session_id") or "default"

            # Record call history
            record = {
                "prompt": prompt,
                "system": system,
                "role": role,
                "session_id": session_id,
                "kwargs": kwargs,
                "timestamp": asyncio.get_event_loop().time(),
            }
            self.call_records.append(record)
            self.history.append({"role": role, "prompt": prompt, "system": system})
            self.sessions.setdefault(session_id, []).append(record)

            combined_text = f"{system or ''}\n\n{prompt}"

            # 1. Evaluate Active Fault Injection Rules
            for idx, frule in enumerate(self.fault_rules):
                if frule.role and frule.role.lower() != role:
                    continue
                if re.search(frule.pattern, combined_text, re.DOTALL | re.IGNORECASE):
                    key = f"fault_{idx}_{frule.fault_type}"
                    self._fault_attempt_counters[key] = self._fault_attempt_counters.get(key, 0) + 1
                    attempt = self._fault_attempt_counters[key]
                    if attempt <= frule.attempts:
                        if frule.fault_type == "rate_limit":
                            raise RuntimeError(
                                f"429 Rate Limit Exceeded. Retry-After: {frule.retry_after}"
                            )
                        elif frule.fault_type in ("auth_error", "unauthorized"):
                            raise RuntimeError(
                                "401 Unauthorized: Invalid API key or expired session token"
                            )
                        elif frule.fault_type == "timeout":
                            raise CLITimeoutError(
                                "Offline mock process timed out after deadline"
                            )
                        elif frule.fault_type == "crash":
                            raise RuntimeError(
                                "CLI process terminated unexpectedly with non-zero exit code 1"
                            )
                        elif frule.fault_type == "corrupt_output":
                            return ProviderResponse(
                                content="\x00corrupted-binary-garbage",
                                usage=TokenUsage(),
                            ).model_dump()

            # 2. Evaluate Scenario Rules
            matched_rule: Optional[MockScenarioRule] = None
            for rule in self.rules:
                if rule.matches(prompt, system=system, role=role):
                    matched_rule = rule
                    break

            if matched_rule:
                rule_key = matched_rule.pattern
                self._attempt_counters[rule_key] = self._attempt_counters.get(rule_key, 0) + 1
                attempt = self._attempt_counters[rule_key]

                if matched_rule.delay_seconds > 0:
                    await asyncio.sleep(matched_rule.delay_seconds)

                if attempt <= matched_rule.fail_attempts and matched_rule.error_type:
                    if matched_rule.error_type == "rate_limit":
                        raise RuntimeError(
                            f"429 Rate Limit Exceeded. Retry-After: {matched_rule.retry_after}"
                        )
                    elif matched_rule.error_type == "auth_error":
                        raise RuntimeError("401 Unauthorized: Invalid token")
                    elif matched_rule.error_type == "timeout":
                        raise CLITimeoutError("Subprocess timeout")
                    elif matched_rule.error_type == "crash":
                        raise RuntimeError("CLI process crashed with exit code 1")

                # Resolve rule response
                if callable(matched_rule.response):
                    content = matched_rule.response(prompt, kwargs)
                elif isinstance(matched_rule.response, BaseModel):
                    content = matched_rule.response.model_dump_json(indent=2)
                elif isinstance(matched_rule.response, dict):
                    content = json.dumps(matched_rule.response, indent=2)
                else:
                    content = str(matched_rule.response)
                usage = matched_rule.token_usage
            elif self.default_response is not None:
                content = self.default_response
                usage = TokenUsage(prompt_tokens=20, completion_tokens=50, total_tokens=70)
            else:
                # 3. Dynamic Default Responses by Role
                usage = TokenUsage(prompt_tokens=50, completion_tokens=150, total_tokens=200)
                if role == "researcher":
                    content = generate_default_researcher_response(prompt)
                elif role == "architect":
                    content = generate_default_architect_response(prompt)
                elif role == "critic":
                    content = generate_default_critic_response(prompt)
                elif role == "coder":
                    content = generate_default_coder_response(prompt)
                elif role == "reviewer":
                    content = generate_default_reviewer_response(prompt)
                elif role == "tester":
                    content = generate_default_tester_response(prompt)
                elif role == "security":
                    content = generate_default_security_response(prompt)
                elif role == "devops":
                    content = generate_default_devops_response(prompt)
                else:
                    content = generate_default_coder_response(prompt)

            response = ProviderResponse(content=content, usage=usage)
            return response.model_dump()


# ---------------------------------------------------------------------------
# Mock Subprocess / CLI Process Simulation
# ---------------------------------------------------------------------------


class MockCLIProcess:
    """Simulates an asyncio.subprocess.Process for spawn_cli interception."""

    def __init__(
        self,
        stdout_bytes: bytes = b"",
        stderr_bytes: bytes = b"",
        returncode: int = 0,
        delay: float = 0.0,
        raise_timeout: bool = False,
        pid: int = 99999,
    ) -> None:
        self.stdout_bytes = stdout_bytes
        self.stderr_bytes = stderr_bytes
        self.returncode = returncode
        self.delay = delay
        self.raise_timeout = raise_timeout
        self.pid = pid
        self._killed = False

    async def communicate(self, input: Optional[bytes] = None) -> Tuple[bytes, bytes]:
        """Simulates process communication and timeout handling."""
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        if self.raise_timeout:
            raise asyncio.TimeoutError("Simulated subprocess timeout")
        return self.stdout_bytes, self.stderr_bytes

    async def wait(self) -> int:
        """Simulates process wait."""
        if self.delay > 0:
            await asyncio.sleep(self.delay)
        return self.returncode

    def kill(self) -> None:
        self._killed = True
        self.returncode = -9

    def terminate(self) -> None:
        self._killed = True
        self.returncode = -15

    def send_signal(self, sig: int) -> None:
        self._killed = True
        self.returncode = -sig


def create_mock_cli_spawn(mock_provider: OfflineMockProvider) -> Callable[..., Any]:
    """
    Creates an async replacement for ag_core.utils.cli_runner.spawn_cli that
    routes all vendor CLI subprocess executions to the mock provider.
    """

    async def mock_spawn_cli(
        cmd: List[str],
        cli_path: str,
        *,
        stdin: Any = asyncio.subprocess.PIPE,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, str]] = None,
    ) -> MockCLIProcess:
        cli_name = str(cli_path).lower()
        cmd_str = " ".join(str(c) for c in cmd)

        # Detect CLI flavor
        if "codex" in cli_name or "codex" in cmd_str:
            flavor = "codex"
        elif "claude" in cli_name or "claude" in cmd_str:
            flavor = "claude"
        elif "grok" in cli_name or "grok" in cmd_str:
            flavor = "grok"
        elif "agy" in cli_name or "gemini" in cli_name or "agy" in cmd_str:
            flavor = "agy"
        elif "nlm" in cli_name or "notebooklm" in cli_name or "nlm" in cmd_str:
            flavor = "notebooklm"
        else:
            flavor = "codex"

        # Handle auxiliary CLI commands (e.g. grok login, nlm list, etc.)
        if "login" in cmd_str:
            return MockCLIProcess(stdout_bytes=b"Login successful\n", returncode=0)
        if flavor == "notebooklm" and "list" in cmd_str:
            return MockCLIProcess(stdout_bytes=CLIStreamEmulator.nlm_notebook_list(), returncode=0)
        if flavor == "notebooklm" and "create" in cmd_str:
            return MockCLIProcess(stdout_bytes=CLIStreamEmulator.nlm_notebook_create(), returncode=0)

        # Extract prompt from temp files or arguments if available
        prompt_content = ""
        for idx, arg in enumerate(cmd):
            if arg == "--prompt-file" and idx + 1 < len(cmd):
                temp_file = cmd[idx + 1]
                try:
                    import os
                    if os.path.exists(temp_file):
                        with open(temp_file, "r", encoding="utf-8", errors="replace") as f:
                            prompt_content = f.read()
                except Exception:
                    pass
                break
            elif arg == "query" and idx + 2 < len(cmd):
                prompt_content = cmd[idx + 2]

        # Standard interactive process wrapper: the prompt is fed during communicate(input=...)
        class DynamicMockCLIProcess(MockCLIProcess):
            def __init__(self, provider: OfflineMockProvider, flavor: str, initial_prompt: str):
                super().__init__()
                self.provider = provider
                self.flavor = flavor
                self.prompt = initial_prompt

            async def communicate(self, input: Optional[bytes] = None) -> Tuple[bytes, bytes]:
                if input is not None:
                    try:
                        stdin_str = input.decode("utf-8", errors="replace")
                        if stdin_str:
                            self.prompt = stdin_str
                    except Exception:
                        pass

                prompt_text = self.prompt or "Generic task prompt"

                try:
                    res = await self.provider.send_prompt(prompt_text)
                    content = res.get("content", "")
                    usage = res.get("usage", {})
                    pt = usage.get("prompt_tokens", 100)
                    ct = usage.get("completion_tokens", 50)
                    stdout_bytes = CLIStreamEmulator.emulate(
                        self.flavor, content, prompt_tokens=pt, completion_tokens=ct
                    )
                    return stdout_bytes, b""
                except CLITimeoutError:
                    raise
                except Exception as exc:
                    err_msg = str(exc)
                    if "429" in err_msg:
                        stderr_bytes = f"HTTP 429 Rate Limit Exceeded. {err_msg}".encode("utf-8")
                        return b"", stderr_bytes
                    elif "401" in err_msg or "Unauthorized" in err_msg:
                        stderr_bytes = f"HTTP 401 Unauthorized. {err_msg}".encode("utf-8")
                        return b"", stderr_bytes
                    else:
                        stderr_bytes = f"Process Error: {err_msg}".encode("utf-8")
                        return b"", stderr_bytes

        return DynamicMockCLIProcess(mock_provider, flavor, prompt_content)

    return mock_spawn_cli


# ---------------------------------------------------------------------------
# Context Manager for Patching CLI Subprocess Layer
# ---------------------------------------------------------------------------


class patch_cli_subprocess:
    """Context manager patching spawn_cli and CLI resolvers to use OfflineMockProvider."""

    def __init__(self, mock_provider: Optional[OfflineMockProvider] = None) -> None:
        self.mock_provider = mock_provider or OfflineMockProvider()
        self._patches: List[Any] = []

    def __enter__(self) -> OfflineMockProvider:
        from unittest.mock import patch

        spawn_fn = create_mock_cli_spawn(self.mock_provider)

        p_spawn = patch("ag_core.utils.cli_runner.spawn_cli", side_effect=spawn_fn)
        p_codex = patch("ag_core.providers.openai_provider.resolve_codex_cli", return_value="mock-codex.exe")
        p_claude = patch("ag_core.providers.anthropic_provider.resolve_claude_cli", return_value="mock-claude.cmd")
        p_grok = patch("ag_core.providers.grok_provider.resolve_grok_cli", return_value="mock-grok.exe")
        p_agy = patch("ag_core.providers.agy_provider.resolve_agy_cli", return_value="mock-agy.exe")
        p_nlm = patch("ag_core.providers.notebooklm_provider.resolve_notebooklm_cli", return_value="mock-nlm.exe")

        self._patches = [p_spawn, p_codex, p_claude, p_grok, p_agy, p_nlm]
        for p in self._patches:
            p.start()

        return self.mock_provider

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._patches.clear()
