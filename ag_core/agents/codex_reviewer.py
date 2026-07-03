import os
from typing import Any
from ag_core.interfaces.base_agent import BaseAgent
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.config import Config, load_config
from ag_core.utils.logger import log_transaction
from ag_core.utils.code_extract import extract_code
from ag_core.utils.cli_runner import communicate_with_timeout, test_timeout


class CodexReviewerAgent(BaseAgent):
    """
    Codex Reviewer Agent that scans project files, performs code review,
    and reports bugs/vulnerabilities.
    """

    def __init__(
        self, provider: BaseProvider, config: Config = None, **kwargs: Any
    ) -> None:
        self.config = config or load_config()
        self.max_retries = kwargs.get("max_retries", 3)
        super().__init__(name="CodexReviewerAgent", provider=provider, **kwargs)

    async def run(
        self, prompt: str | None = None, context_data: dict | None = None
    ) -> str:
        user_prompt = (
            prompt
            or self.extra_params.get("prompt")
            or "Perform a code review of the project files, checking for bugs, style issues, and security vulnerabilities."
        )

        # Parse and wrap specialized slash commands. /code and /refactor are
        # GENERATION requests: the caller (orchestrator / MCP tool) writes and
        # verifies the produced file itself, so this agent must return the
        # model output untouched — no lint/test verification loop, and no log
        # sections appended (a real run poisoned extract_code downstream: the
        # appended pytest log of this host repo's own suite was the largest
        # fenced block and got extracted instead of the code).
        generation_mode = False
        words = user_prompt.strip().split(maxsplit=1)
        if words and words[0].startswith("/"):
            cmd = words[0]
            query = words[1] if len(words) > 1 else ""
            if cmd in ("/code", "/refactor"):
                generation_mode = True
            if cmd == "/code":
                user_prompt = f"Write clean, robust, and well-documented code for the following request:\n\n{query}"
            elif cmd == "/refactor":
                user_prompt = f"Refactor the existing code or components to improve readability, performance, and structure, explaining the changes made:\n\n{query}"
            elif cmd == "/security":
                user_prompt = f"Perform a security code audit, looking for vulnerabilities, insecure practices, data leaks, or potential attack vectors:\n\n{query}"

        # Scan project files (or use provided context_data) and format context
        root_dir = os.getcwd()
        scanned_files, context = await self.scan_context_async(context_data)

        # Retrieve matching past interactions
        past_memories = await self.retrieve_memory_async(user_prompt, limit=3)
        memory_context = ""
        if past_memories:
            memory_context = "\n--- Relevant Historical Memory Context ---\n"
            for i, mem in enumerate(past_memories, 1):
                memory_context += f"Interaction #{i}:\n{mem['text']}\n"

        history_context = self.format_history()

        full_prompt = f"{history_context}{user_prompt}\n"
        if memory_context:
            full_prompt += f"{memory_context}\n"
        full_prompt += f"\nProject files context:\n{context}"

        from ag_core.utils.prompt_templates import CODER_PROMPT

        # Invoke provider
        response = await self.provider.send_prompt(full_prompt, system=CODER_PROMPT)
        content = response.get("content", "")
        usage = response.get("usage", {})

        self.history.append({"prompt": user_prompt, "response": content})

        # Save interaction to memory
        await self.store_memory_async(
            text=f"Prompt: {user_prompt}\nResponse: {content}",
            metadata={
                "type": "agent_run",
                "task_id": self.extra_params.get("task_id", "unknown"),
            },
        )

        # Log transaction
        log_transaction(
            model_name=self.provider.model_name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )

        # Generation requests return the model output untouched — the caller
        # writes and verifies the file (see the slash-command comment above).
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
        if pytest_exit_code != 0:
            for attempt in range(1, self.max_retries + 1):
                target_file = _detect_target_file(
                    user_prompt, content, scanned_files.keys()
                )

                retry_prompt = (
                    f"The test suite failed with exit code {pytest_exit_code}.\n"
                    f"Test logs:\n{pytest_logs}\n\n"
                    f"Please fix the bugs in the code. Original prompt: {user_prompt}\n\n"
                    "Do NOT run tests, commands, or tools. Output ONLY the "
                    "complete file content in a single ```python fenced block."
                )
                response = await self.provider.send_prompt(
                    retry_prompt, system=CODER_PROMPT
                )
                content = response.get("content", "")
                usage = response.get("usage", {})
                log_transaction(
                    model_name=self.provider.model_name,
                    prompt_tokens=usage.get("prompt_tokens", 0),
                    completion_tokens=usage.get("completion_tokens", 0),
                )

                code_to_write = extract_code(content)
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
                    try:
                        os.makedirs(os.path.dirname(abs_target_path), exist_ok=True)
                        with open(abs_target_path, "w", encoding="utf-8") as f:
                            f.write(code_to_write)
                    except Exception as e:
                        print(
                            f"Warning: Failed to write back fixed code to {abs_target_path}: {e}"
                        )

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

                if pytest_exit_code == 0:
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
        self.write_output(self.resolve_output_file("review.md"), content)
