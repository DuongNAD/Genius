import os
from typing import Any
from ag_core.interfaces.base_agent import BaseAgent
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.config import Config, load_config
from ag_core.utils.code_extract import extract_code, fence_hint
from ag_core.utils.cli_runner import communicate_with_timeout, test_timeout

# Imported into THIS namespace (not used via BaseAgent._log_usage): tests
# patch ``ag_core.agents.codex_reviewer.log_transaction`` to silence the
# usage-logging side effect, so the module-level name must stay.
from ag_core.utils.logger import log_transaction


# R5 Wave 5: opt-in code-preservation discipline appended to the coder
# system prompt for generation requests. Off by default (env unset), so the
# system prompt stays byte-identical to the pre-R5 behavior tests pin.
SURGICAL_EDIT_GUIDANCE = (
    "\n\n## Code preservation (surgical edits)\n"
    "When modifying existing code, change ONLY what the request requires:\n"
    "- Identify the exact lines/expressions to change; leave everything else "
    "byte-for-byte identical, including comments, formatting, and unrelated "
    "configuration values.\n"
    "- NEVER change model names, API keys, version pins, or unrelated config "
    "unless explicitly asked to.\n"
    "- Prefer the smallest correct diff over a rewrite; before finishing, "
    "verify the surrounding code is untouched.\n"
)


def _surgical_edits_enabled() -> bool:
    """Whether opt-in surgical-edit guidance augments the coder prompt."""
    return os.getenv("GENIUS_SURGICAL_EDITS", "").lower() in ("1", "true", "yes")


# pytest exit codes: 0 = all passed, 5 = no tests were collected. Reviewing a
# workspace that simply has no tests must NOT be treated as a test failure —
# doing so drives the self-heal loop (and used to rewrite the reviewed file)
# for no reason. Treat both as "review passed".
_PYTEST_REVIEW_OK_EXIT_CODES = (0, 5)


def _pytest_review_passed(exit_code: int) -> bool:
    return exit_code in _PYTEST_REVIEW_OK_EXIT_CODES


def _safe_write_back(abs_target_path: str, code_to_write: str) -> bool:
    """Write model-regenerated code back to the reviewed file during the
    self-heal loop — but NEVER let a code-less retry destroy it.

    A retry can return no extractable code: prose, a refusal, or an empty
    result from a partially-failed FallbackProvider all make ``extract_code``
    return ``""``. Writing that would truncate the user's source file to zero
    bytes (or overwrite it with prose). Guard against it: skip the write when
    there is nothing to write, leaving the original file intact so the loop can
    retry. Returns True iff the file was actually written.
    """
    if not code_to_write.strip():
        print(
            f"Warning: skipping write-back to {abs_target_path}: "
            "model returned no extractable code."
        )
        return False
    try:
        os.makedirs(os.path.dirname(abs_target_path), exist_ok=True)
        with open(abs_target_path, "w", encoding="utf-8") as f:
            f.write(code_to_write)
        return True
    except Exception as e:
        print(f"Warning: Failed to write back fixed code to {abs_target_path}: {e}")
        return False


class CodexReviewerAgent(BaseAgent):
    """
    Codex Reviewer Agent that scans project files, performs code review,
    and reports bugs/vulnerabilities.
    """

    DEFAULT_TASK = (
        "Perform a code review of the project files, checking for bugs, "
        "style issues, and security vulnerabilities."
    )
    # /code and /refactor are GENERATION requests: the caller (orchestrator /
    # MCP tool) writes and verifies the produced file itself, so this agent
    # must return the model output untouched — no lint/test verification
    # loop, and no log sections appended (a real run poisoned extract_code
    # downstream: the appended pytest log of this host repo's own suite was
    # the largest fenced block and got extracted instead of the code).
    GENERATION_COMMANDS = ("/code", "/refactor")
    # Output is parsed by extract_code + ast.parse -> effort only; format/
    # variants are excluded so they can never perturb the ```python``` block.
    ACCEPTED_MODIFIERS = frozenset({"deep"})
    SLASH_PREFIXES = {
        "/code": "Write clean, robust, and well-documented code for the following request:\n\n",
        "/refactor": "Refactor the existing code or components to improve readability, performance, and structure, explaining the changes made:\n\n",
        "/security": "Perform a security code audit, looking for vulnerabilities, insecure practices, data leaks, or potential attack vectors:\n\n",
    }
    USES_MEMORY = True
    DEFAULT_OUTPUT_FILE = "review.md"

    def __init__(
        self, provider: BaseProvider, config: Config = None, **kwargs: Any
    ) -> None:
        self.config = config or load_config()
        self.max_retries = kwargs.get("max_retries", 3)
        super().__init__(name="CodexReviewerAgent", provider=provider, **kwargs)

    async def run(
        self,
        prompt: str | None = None,
        context_data: dict | None = None,
        *,
        effort: str | None = None,
    ) -> str:
        user_prompt, cmd = self._route_slash_command(self._resolve_user_prompt(prompt))
        effort = effort or self.directives.effort
        generation_mode = cmd in self.GENERATION_COMMANDS

        # Scan project files (or use provided context_data) and format context
        root_dir = os.getcwd()
        scanned_files, context = await self.scan_context_async(context_data)

        memory_context = await self._memory_context_block(user_prompt)
        full_prompt = self._compose_full_prompt(user_prompt, memory_context, context)

        from ag_core.utils.prompt_templates import CODER_PROMPT

        # Opt-in surgical-edit mode (GENIUS_SURGICAL_EDITS): for generation
        # requests, ask for minimal, targeted diffs and forbid touching model
        # names / API keys / unrelated config. Off by default, so the system
        # prompt is byte-identical to before unless explicitly enabled.
        system_prompt = CODER_PROMPT
        if generation_mode and _surgical_edits_enabled():
            system_prompt = CODER_PROMPT + SURGICAL_EDIT_GUIDANCE

        # Invoke provider
        response = await self.provider.send_prompt(
            full_prompt, system=system_prompt, effort=effort
        )
        content = response.get("content", "")
        usage = response.get("usage", {})

        self.history.append({"prompt": user_prompt, "response": content})

        await self._store_run_memory(user_prompt, content)
        log_transaction(
            model_name=self.provider.model_name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )

        # Generation requests return the model output untouched — the caller
        # writes and verifies the file (see the GENERATION_COMMANDS comment).
        if generation_mode:
            self._write_output_file(content)
            return content

        # In stateless (skill-server) mode the agent must leave no trace and
        # must NOT execute the host's test suite: skip the flake8/pytest
        # self-healing loop below, which writes model-generated code into the
        # server's working tree and re-runs pytest — a remote-code-execution
        # surface. Return the model's review text directly.
        if bool(self.extra_params.get("stateless", False)):
            self._write_output_file(content)  # no-op when output_file == "None"
            return content

        def _detect_target_file(prompt_str, content_str, scanned_keys):
            import re

            m = re.search(r"(?:#|//)\s*(?:filepath|path):\s*([^\s\n\r]+)", content_str)
            if m:
                return m.group(1).strip()
            m = re.search(r"[\"']?([a-zA-Z0-9_\-\./]+\.py)[\"']?", prompt_str)
            if m:
                return m.group(1).strip()
            py_files = [f for f in scanned_keys if f.endswith(".py")]
            if len(py_files) == 1:
                return py_files[0]
            return None

        # 1. Run flake8 on the files being reviewed
        import sys
        import asyncio

        python_files = []
        for f in scanned_files.keys():
            if f.endswith(".py"):
                abs_p = os.path.abspath(os.path.join(root_dir, f))
                if os.path.exists(abs_p):
                    python_files.append(abs_p)

        linter_findings = ""
        if "PYTEST_CURRENT_TEST" in os.environ:
            linter_findings = "Mocked linter findings for test"
        elif python_files:
            flake8_cmd = [sys.executable, "-m", "flake8"] + python_files
            try:
                process = await asyncio.create_subprocess_exec(
                    *flake8_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                stdout, stderr = await communicate_with_timeout(
                    process, timeout=test_timeout(), cli_name="verification"
                )
                linter_findings = (
                    stdout.decode("utf-8", errors="replace")
                    + "\n"
                    + stderr.decode("utf-8", errors="replace")
                )
            except Exception as e:
                linter_findings = f"Failed to run flake8: {e}"

        # 2. Run pytest on the test suite using sys.executable -m pytest
        pytest_cmd = [sys.executable, "-m", "pytest"]
        env = os.environ.copy()
        project_dir = os.path.abspath(root_dir)
        project_src_dir = os.path.join(project_dir, "src")
        env["PYTHONPATH"] = os.path.pathsep.join(
            [project_dir, project_src_dir, env.get("PYTHONPATH", "")]
        ).strip(os.path.pathsep)

        if "PYTEST_CURRENT_TEST" in os.environ:
            pytest_exit_code = 0
            pytest_logs = "Mocked pytest logs for test"
        else:
            try:
                process = await asyncio.create_subprocess_exec(
                    *pytest_cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=env,
                )
                stdout, stderr = await communicate_with_timeout(
                    process, timeout=test_timeout(), cli_name="verification"
                )
                pytest_exit_code = process.returncode
                pytest_logs = (
                    stdout.decode("utf-8", errors="replace")
                    + "\n"
                    + stderr.decode("utf-8", errors="replace")
                )
            except Exception as e:
                pytest_exit_code = -999
                pytest_logs = f"Failed to run pytest: {e}"

        # 3. If tests fail, run a self-healing loop to let Codex fix the bugs, write back to file, and verify.
        if not _pytest_review_passed(pytest_exit_code):
            for attempt in range(1, self.max_retries + 1):
                target_file = _detect_target_file(
                    user_prompt, content, scanned_files.keys()
                )

                retry_prompt = (
                    f"The test suite failed with exit code {pytest_exit_code}.\n"
                    f"Test logs:\n{pytest_logs}\n\n"
                    f"Please fix the bugs in the code. Original prompt: {user_prompt}\n\n"
                    "Do NOT run tests, commands, or tools. Output ONLY the "
                    f"complete file content in a single {fence_hint(target_file)}."
                )
                response = await self.provider.send_prompt(
                    retry_prompt, system=CODER_PROMPT, effort=effort
                )
                content = response.get("content", "")
                usage = response.get("usage", {})
                log_transaction(
                    model_name=self.provider.model_name,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                )

                code_to_write = extract_code(content, filename=target_file)
                if target_file:
                    # realpath (not abspath) so an in-tree symlink can't point
                    # the write outside root_dir.
                    abs_target_path = os.path.realpath(
                        os.path.join(root_dir, target_file)
                    )
                    try:
                        abs_root = os.path.realpath(root_dir)
                        if os.path.commonpath([abs_root, abs_target_path]) != abs_root:
                            raise ValueError(
                                "Path traversal detected: target path is outside root directory"
                            )
                    except ValueError as e:
                        raise ValueError(f"Path traversal detected: {e}")
                    _safe_write_back(abs_target_path, code_to_write)

                # Verify again
                try:
                    process = await asyncio.create_subprocess_exec(
                        *pytest_cmd,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        env=env,
                    )
                    stdout, stderr = await communicate_with_timeout(
                        process, timeout=test_timeout(), cli_name="verification"
                    )
                    pytest_exit_code = process.returncode
                    pytest_logs = (
                        stdout.decode("utf-8", errors="replace")
                        + "\n"
                        + stderr.decode("utf-8", errors="replace")
                    )
                except Exception as e:
                    pytest_exit_code = -999
                    pytest_logs = f"Failed to run pytest: {e}"

                if python_files:
                    try:
                        process = await asyncio.create_subprocess_exec(
                            *flake8_cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                        )
                        stdout, stderr = await communicate_with_timeout(
                            process, timeout=test_timeout(), cli_name="verification"
                        )
                        linter_findings = (
                            stdout.decode("utf-8", errors="replace")
                            + "\n"
                            + stderr.decode("utf-8", errors="replace")
                        )
                    except Exception as e:
                        linter_findings = f"Failed to run flake8: {e}"

                if _pytest_review_passed(pytest_exit_code):
                    break

        # Append linter findings and test logs to the final returned review output
        content = (
            content
            + f"\n\n### Linter Findings (flake8)\n```\n{linter_findings}\n```\n\n### Pytest Logs\n```\n{pytest_logs}\n```"
        )

        self._write_output_file(content)

        return content

    def _write_output_file(self, content: str) -> None:
        # Thin wrapper over the base helpers (same "None" sentinel handling).
        self.write_output(self.resolve_output_file(self.DEFAULT_OUTPUT_FILE), content)
