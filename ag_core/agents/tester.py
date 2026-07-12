import os
from typing import Any
from ag_core.interfaces.base_agent import BaseAgent
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.config import Config, load_config
from ag_core.utils.code_extract import extract_code
from ag_core.utils.cli_runner import communicate_with_timeout, test_timeout


class TesterAgent(BaseAgent):
    """
    Tester Agent that receives Codex's review output, scans project files,
    and writes automatically generated unit tests/scenarios.
    """

    __test__ = False

    DEFAULT_TASK = (
        "Generate unit tests and scenarios based on the review output "
        "and project files."
    )
    SLASH_PREFIXES = {
        "/unit-test": "Generate comprehensive unit tests and test suites using pytest for the project files context, focusing on edge cases and validation:\n\n",
        "/stress-test": "Create a performance or stress testing script or scenario to simulate heavy concurrent load, analyzing latency and failure modes:\n\n",
    }
    # Output is written AND executed as a pytest module -> effort only; nothing
    # that could perturb the runnable ```python``` block.
    ACCEPTED_MODIFIERS = frozenset({"deep"})
    USES_MEMORY = False
    DEFAULT_OUTPUT_FILE = "test_generated.py"

    def __init__(
        self, provider: BaseProvider, config: Config = None, **kwargs: Any
    ) -> None:
        self.config = config or load_config()
        self.max_retries = kwargs.get("max_retries", 3)
        super().__init__(name="TesterAgent", provider=provider, **kwargs)

    async def run(
        self,
        prompt: str | None = None,
        context_data: dict | None = None,
        *,
        effort: str | None = None,
    ) -> str:
        user_prompt, _ = self._route_slash_command(self._resolve_user_prompt(prompt))
        effort = effort or self.directives.effort

        # Scan project files (or use provided context_data) and format context
        _, context = await self.scan_context_async(context_data)
        full_prompt = self._compose_full_prompt(user_prompt, "", context)

        from ag_core.utils.prompt_templates import TESTER_PROMPT

        # Invoke provider
        response = await self.provider.send_prompt(
            full_prompt, system=TESTER_PROMPT, effort=effort
        )
        content = response.get("content", "")
        usage = response.get("usage", {})

        self._log_usage(usage)

        # Write to output file
        output_file = self.resolve_output_file(self.DEFAULT_OUTPUT_FILE)

        test_failures_logs = ""
        if output_file != "None":
            # Self-healing loop
            for attempt in range(1, self.max_retries + 1):
                code_to_write = extract_code(content, filename=output_file)
                self.write_output(output_file, code_to_write)

                import sys
                import asyncio

                pytest_cmd = [sys.executable, "-m", "pytest", output_file]
                env = os.environ.copy()
                abs_output_file = os.path.abspath(output_file)
                project_dir = os.path.dirname(os.path.dirname(abs_output_file))
                project_src_dir = os.path.join(project_dir, "src")
                env["PYTHONPATH"] = os.path.pathsep.join(
                    [project_dir, project_src_dir, env.get("PYTHONPATH", "")]
                ).strip(os.path.pathsep)

                if "PYTEST_CURRENT_TEST" in os.environ:
                    exit_code = 0
                    test_failures_logs = "Mocked pytest logs for test"
                else:
                    try:
                        process = await asyncio.create_subprocess_exec(
                            *pytest_cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            env=env,
                        )
                        # Bounded so a hung generated test can't freeze the
                        # self-heal loop forever; the except below treats a
                        # timeout as a failed run and steers the next attempt.
                        stdout, stderr = await communicate_with_timeout(
                            process, timeout=test_timeout(), cli_name="pytest"
                        )
                        exit_code = process.returncode
                        test_failures_logs = (
                            stdout.decode("utf-8", errors="replace")
                            + "\n"
                            + stderr.decode("utf-8", errors="replace")
                        )
                    except Exception as e:
                        exit_code = -999
                        test_failures_logs = f"Failed to run pytest: {e}"

                if exit_code == 0:
                    break
                else:
                    retry_prompt = (
                        f"The previously generated test code failed to run. Pytest exit code: {exit_code}.\n"
                        f"Error logs:\n{test_failures_logs}\n\n"
                        f"Please fix the test code and return it. Original context:\n{full_prompt}"
                    )
                    response = await self.provider.send_prompt(
                        retry_prompt, system=TESTER_PROMPT, effort=effort
                    )
                    content = response.get("content", "")
                    usage = response.get("usage", {})
                    self._log_usage(usage)

            # Make sure the final clean code without evidence remains written in output_file
            code_to_write = extract_code(content, filename=output_file)
            self.write_output(output_file, code_to_write)

            # Append the test execution evidence (pytest stdout/stderr) to the returned markdown response
            content = (
                content
                + f"\n\n### Pytest Execution Evidence\n```\n{test_failures_logs}\n```"
            )

        self.history.append({"prompt": user_prompt, "response": content})
        return content
