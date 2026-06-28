# Upgrade Plan: Standardized Prompt Framework Integration

This document outlines the upgrade plan for integrating a standardized prompt engineering framework into the Genius microservices architecture. It maps the rules and principles of AI-Native Engineering to the Genius codebase, proposes a Standardized Prompt Object (SPO) schema, provides concrete examples, and defines the files and code flows to be upgraded.

---

## 1. Detailed Analysis of Principles & Guidelines

Based on the files in `C:\Users\Admin\Downloads\AGENTS` (`AGENTS.md`, `Beforehand_utf8.txt`, `Cheatsheet_utf8.txt`), the following analysis highlights the core principles of AI-Native Engineering and evaluates how they map onto the current Genius microservices architecture:

### A. Separation of Planning and Implementation (Plan vs. Implement)
* **Principle**: Agents must formulate and align on a detailed, step-by-step plan (directories, files, and sequence of modifications) before writing any code. Skipping this step leads to expensive, incorrect, multi-file edits.
* **Genius Mapping**: 
  * Currently, the orchestrator divides the pipeline into research (`GrokResearcherAgent`), design (`ClaudeArchitectAgent`), and implementation (`CodexReviewerAgent`).
  * In `claude_architect.py`, the agent appends a plan constraint: `sys_prompt = AGENT_CORE_RULES + "\nTách plan và implement (Separate plan and implement)..."`.
  * *Improvement*: The plan-versus-implement distinction should be explicitly defined in the metadata and instructions of the prompt request, rather than using string concatenation.

### B. Strict Verification Feedback Loop
* **Principle**: Prompt payloads should include verification details—such as test commands (e.g., `pytest`), linter checks (e.g., `flake8`), and expected outputs. When the agent sees logs of failed verification steps, it can self-correct without human intervention.
* **Genius Mapping**: 
  * The `orchestrator.py` implements a self-healing loop inside `process_single_file()`, where it executes `pytest` and, if it fails, re-prompts the Codex agent with test failures and logs for up to `max_retries` attempts.
  * *Improvement*: The test failures, compiler logs, and linter outputs are currently appended as raw strings to the user prompt. We should separate this into a designated `feedback_loop` block in our structured prompt.

### C. Problem-Focused, Not Solution-Prescribed
* **Principle**: Prompts should describe the problem domain, requirements, and constraints (e.g., "Implement a login form with validation") rather than locking the agent into a pre-selected implementation path (e.g., "Use React useState for login form").
* **Genius Mapping**: 
  * Individual agents (`CodexReviewerAgent`, `TesterAgent`, etc.) currently intercept user prompts and prefix them with instructions like `/code`, `/unit-test`, or `/audit`.
  * *Improvement*: The problem description should be isolated in a dedicated `payload` field to prevent target implementation paths from being hardcoded.

### D. One Task, One Prompt (Context Discipline)
* **Principle**: Never combine multiple unrelated requests in a single prompt session. Keep prompt contexts clean to prevent degradation, which begins around 30-40% context window utilization.
* **Genius Mapping**: 
  * In `orchestrator.py`, the orchestration pipeline parses `design.md` and processes files one by one using a worker pool and `process_single_file()`. This aligns with the "One Task, One Prompt" rule.
  * *Improvement*: Standardizing the task metadata (e.g., `task_id`) helps trace tasks across multiple agents and ensures that each sub-request stays isolated.

### E. Anti-Over-engineering Constraints
* **Principle**: Set strict limits on adding extra abstractions, helper scripts, or external dependencies. Prefer modifying existing files, keeping functions under 50 lines, and strictly avoiding raw `.env` file queries.
* **Genius Mapping**: 
  * The codebase defines standard rules in `ag_core/utils/prompt_templates.py` under the `AGENT_CORE_RULES` string.
  * *Improvement*: Instead of passing a large monolithic system prompt string, constraints should be represented as an array in the prompt structure, enabling dynamic toggling of rules based on the agent's target role.

### F. Rewind and Re-Prompt over Error Stacking
* **Principle**: If an agent goes down the wrong path, it is better to reset ("rewind") the session context to a clean state and adjust the prompt rather than trying to patch errors incrementally.
* **Genius Mapping**: 
  * The self-healing loop in `orchestrator.py` currently appends new error logs to the existing conversation context, which increases noise and leads to error stacking.
  * *Improvement*: The metadata of the prompt structure should track the `attempt` count, allowing the model and the agent provider to decide when to perform a clean context rewind.

---

## 2. Proposed Standardized Prompt Format Design

To programmatically enforce prompt engineering rules, we propose replacing the unstructured `prompt` string with a structured **Standardized Prompt Object (SPO)** schema.

### JSON Schema Specification
```json
{
  "$schema": "http://json-schema.org/draft-07/schema#",
  "title": "StandardizedPromptObject",
  "type": "object",
  "required": ["meta", "instructions", "payload"],
  "properties": {
    "meta": {
      "type": "object",
      "required": ["task_id", "caller_agent", "target_role", "attempt"],
      "properties": {
        "task_id": { "type": "string" },
        "caller_agent": { "type": "string" },
        "target_role": { "type": "string" },
        "attempt": { "type": "integer", "minimum": 1 },
        "max_attempts": { "type": "integer", "minimum": 1 }
      }
    },
    "instructions": {
      "type": "object",
      "required": ["system_rules"],
      "properties": {
        "system_rules": { "type": "string" },
        "role_specific_instructions": { "type": "string" },
        "constraints": {
          "type": "array",
          "items": { "type": "string" }
        },
        "verification_command": { "type": "string" }
      }
    },
    "payload": {
      "type": "object",
      "required": ["problem_description"],
      "properties": {
        "problem_description": { "type": "string" },
        "slash_command": { "type": "string" },
        "target_file": { "type": "string" },
        "reference_code": { "type": "string" }
      }
    },
    "feedback_loop": {
      "type": "object",
      "properties": {
        "previous_errors": { "type": "string" },
        "test_logs": { "type": "string" },
        "criticism": { "type": "string" }
      }
    }
  }
}
```

### Design Justifications

1. **The `meta` Block**:
   * **Justification**: Tracks execution ID (`task_id`) and agent lineage. This is essential for monitoring multi-agent pipelines.
   * **Attempt Tracking**: Tracking `attempt` lets the system implement the **Rewind and Re-Prompt** principle. If `attempt > 1`, the provider can prune the history of previous failures to avoid polluting the context window.

2. **The `instructions` Block**:
   * **Justification**: Standardizes how system rules and constraints are passed.
   * **Dynamic Constraints**: Allows injecting rules (like "Keep functions under 50 lines", "Do not read .env file", "Co-locate tests") dynamically.
   * **Verification Command**: Explicitly states how the work will be verified, giving the agent a clear target for self-correction.

3. **The `payload` Block**:
   * **Justification**: Separates the task requirements (problem-focused) from configuration and meta parameters.
   * **Reference Code isolation**: Isolating code snippets prevents confusing the agent between code-to-be-modified and examples.

4. **The `feedback_loop` Block**:
   * **Justification**: Separates criticism and execution logs from the core problem statement.
   * **Self-Healing**: Facilitates self-healing by allowing the compiler errors, test logs, and security audits to be injected cleanly.

---

## 3. Concrete Example of the Standardized Prompt Object

### A. The JSON Representation
The following is an example of an SPO constructed by `orchestrator.py` during Attempt #2 of implementing a JWT utility class:

```json
{
  "meta": {
    "task_id": "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d",
    "caller_agent": "orchestrator",
    "target_role": "codex_reviewer",
    "attempt": 2,
    "max_attempts": 3
  },
  "instructions": {
    "system_rules": "AI-Native Engineering Rules:\n1. One Task at a Time\n2. Build, Test, and Lint\n3. Code Execution Proof\n4. No .env Access\n5. No Over-Engineering\n6. Internal Communication in English\n7. User Communication in Vietnamese",
    "role_specific_instructions": "Write clean, robust, and well-documented code. Separate plan and implementation. Do not write code until the plan is aligned.",
    "constraints": [
      "Keep functions under 50 lines",
      "Do not read raw .env files directly",
      "Co-locate tests next to source files"
    ],
    "verification_command": "pytest tests/test_jwt.py"
  },
  "payload": {
    "problem_description": "Implement a helper module `ag_core/utils/jwt.py` containing function `decode_jwt(token: str, secret: str) -> dict` and `encode_jwt(payload: dict, secret: str) -> str`.",
    "slash_command": "/code",
    "target_file": "ag_core/utils/jwt.py",
    "reference_code": "def encode_jwt(payload: dict, secret: str) -> str:\n    # Reference pattern implementation\n    return jwt.encode(payload, secret, algorithm='HS256')"
  },
  "feedback_loop": {
    "previous_errors": "flake8: ag_core/utils/jwt.py:12:80: E501 line too long (82 > 79 characters)",
    "test_logs": "pytest: test_jwt.py:15: AssertionError: expected {'sub': '123'} but got {'sub': '123', 'exp': 1719600000}",
    "criticism": "Security check passed. No vulnerabilities detected."
  }
}
```

### B. Rendered System Prompt
The `PromptBuilder` compiles the SPO into the following System Prompt sent to the local CLI provider:

```text
AI-Native Engineering Rules:
1. One Task at a Time: Focus on solving exactly one task at a time. Do not attempt to process multiple independent files or tasks concurrently.
2. Build, Test, and Lint: Always run build commands, tests, and linters (such as flake8) after modifications to verify code correctness. Never assume code is correct without execution validation.
3. Code Execution Proof: Provide clear, concrete evidence/proof of code execution, including test logs and linter results.
4. No .env Access: Do not read, query, or attempt to parse `.env` files or raw environment secret keys. Use designated config loaders or environment injection.
5. No Over-Engineering: Implement only what is requested in a simple, robust manner. Avoid unnecessary complexity, extra layers, or over-engineered abstractions.
6. Internal Communication: Always communicate internally with other agents, write code comments, and output technical logs in English.
7. User Communication: When generating output intended for the end-user, always respond in Vietnamese (Tiếng Việt).

ROLE INSTRUCTIONS:
Write clean, robust, and well-documented code. Separate plan and implementation. Do not write code until the plan is aligned.

CONSTRAINTS:
- Keep functions under 50 lines
- Do not read raw .env files directly
- Co-locate tests next to source files

VERIFICATION REQUIREMENT:
You must ensure your implementation is validated by running: pytest tests/test_jwt.py
```

### C. Rendered User Prompt
The `PromptBuilder` compiles the SPO into the following User Prompt sent to the local CLI provider:

```text
SLASH COMMAND: /code

TARGET FILE: ag_core/utils/jwt.py

TASK DESCRIPTION:
Implement a helper module `ag_core/utils/jwt.py` containing function `decode_jwt(token: str, secret: str) -> dict` and `encode_jwt(payload: dict, secret: str) -> str`.

REFERENCE CODE:
```python
def encode_jwt(payload: dict, secret: str) -> str:
    # Reference pattern implementation
    return jwt.encode(payload, secret, algorithm='HS256')
```

--- FEEDBACK FROM ATTEMPT #1 ---
Linter/Compiler Errors:
flake8: ag_core/utils/jwt.py:12:80: E501 line too long (82 > 79 characters)

Test Execution Logs:
pytest: test_jwt.py:15: AssertionError: expected {'sub': '123'} but got {'sub': '123', 'exp': 1719600000}

Criticism / Review Feedback:
Security check passed. No vulnerabilities detected.

Please repair the code based on the feedback above.
```

---

## 4. List of Files and Code Flows in Genius to be Upgraded

### A. Upgraded Files Index

| File Path | Component | Description of Changes |
|---|---|---|
| `ag_core/utils/prompt_templates.py` | Prompt Utilities | Introduce `PromptBuilder` class to validate and compile Standardized Prompt Objects (SPOs) into system and user prompts. |
| `ag_core/interfaces/base_agent.py` | Core Agent Interface | Modify `BaseAgent.run(prompt, context_data)` to accept both `str` and `dict` (SPO) arguments. Implement auto-wrapping for backward compatibility. |
| `ag_core/agents/claude_architect.py` | Design Agent | Retrieve instruction fields, roles, and constraints from the SPO. Pass the structured system prompt instead of using raw string concatenation. |
| `ag_core/agents/codex_reviewer.py` | Implementation Agent | Parse payload block, target file, and reference code. Feed attempt history and `feedback_loop` data into the provider. |
| `ag_core/agents/tester.py` | Testing Agent | Retrieve targets, format the test generation user prompt using the feedback block, and request verification logs. |
| `ag_core/agents/security_agent.py` | Security Audit Agent | Access payload elements and execute audit tasks utilizing structured constraints. |
| `ag_core/agents/devops_agent.py` | Deployment Agent | Leverage system rules and deployment metadata within the SPO wrapper. |
| `orchestrator.py` | Orchestration Pipeline | Refactor `process_single_file()` to instantiate the SPO dictionary, increment `meta.attempt` dynamically, and populate `feedback_loop` with pytest logs. |
| `serve.py` | FastAPI Hub Server | Extend request schemas to support validating the SPO JSON schema on HTTP routes and WebSocket message processors. |
| `ag_core/distributed/worker.py` | Distributed Workers | Update `execute_task()` to pass the structured JSON payload downstream to base agent wrappers. |

### B. Detailed Code Flows to be Upgraded

#### 1. Task Dispatching Flow in `orchestrator.py`
* **Current State**:
  * `codex_req_prompt` is created by appending specification and feedback strings.
  * The orchestrator calls the local CLI wrapper directly (e.g., `codex_cli_invoke(codex_req_prompt, context=current_context, ...)`).
* **Upgraded Flow**:
  * Instantiate a structured dictionary matching the SPO schema at the start of `process_single_file()`.
  * Set `meta.task_id`, `meta.caller_agent = "orchestrator"`, and `meta.attempt = attempt`.
  * Populate `instructions.system_rules` with `AGENT_CORE_RULES`, and add relevant coding standard rules as constraints.
  * Assign target files, specification text, and slash commands inside `payload`.
  * For retries (where `attempt > 1`), copy current `test_failures_logs` into `feedback_loop.test_logs`, and linter failures into `feedback_loop.previous_errors`.
  * Call the relevant local CLI wrapper method with the serializable SPO dictionary payload.

#### 2. Agent Execution Flow in `BaseAgent` and Individual Agents
* **Current State**:
  * `BaseAgent.run` expects a raw string prompt.
  * Individual agents (`CodexReviewerAgent`, `ClaudeArchitectAgent`, etc.) perform manual string splits (e.g., `user_prompt.strip().split(maxsplit=1)`) to parse slash commands, and then invoke `self.provider.send_prompt(full_prompt, system=AGENT_CORE_RULES)`.
* **Upgraded Flow**:
  * `BaseAgent.run()` intercepts incoming prompt payloads. If it is a dictionary matching the SPO schema, it routes it to the upgraded parsing pipeline. If it is a string, it wraps it in a default SPO for backward compatibility.
  * The agent invokes `PromptBuilder.build_system_prompt(spo)` and `PromptBuilder.build_user_prompt(spo)`.
  * The rendered prompts are passed to `self.provider.send_prompt(user_prompt, system=system_prompt)`.

#### 3. Worker API and Communication Flow
* **Current State**:
  * `serve.py` and `worker.py` validate payloads with `X-Payload-SHA256` matching the SHA256 checksum of the string request body.
  * `execute_task` in `worker.py` parses `task_data.get("prompt")` as a string and forwards it to the agent.
* **Upgraded Flow**:
  * `serve.py` route validators accept JSON objects satisfying the SPO schema.
  * `ClientWorker.execute_task` checks if `task_data.get("prompt")` is a dictionary, and passes the deserialized object directly to the agent's `run()` method.
  * Checksum validation helpers are updated to serialize the sorted keys of the SPO dictionary before calculating the SHA256 signature to guarantee consistency across communication channels.
