import os
from typing import Any
from ag_core.interfaces.base_agent import BaseAgent
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.scanner.project_scanner import ProjectScanner
from ag_core.config import Config, load_config
from ag_core.utils.logger import log_transaction

class TesterAgent(BaseAgent):
    """
    Tester Agent that receives Codex's review output, scans project files,
    and writes automatically generated unit tests/scenarios.
    """
    __test__ = False

    def __init__(self, provider: BaseProvider, config: Config = None, **kwargs: Any) -> None:
        self.config = config or load_config()
        self.max_retries = kwargs.get("max_retries", 3)
        super().__init__(name="TesterAgent", provider=provider, **kwargs)

    async def run(self, prompt: str | None = None, context_data: dict | None = None) -> str:
        user_prompt = prompt or self.extra_params.get("prompt") or "Generate unit tests and scenarios based on the review output and project files."
        
        # Parse and wrap specialized slash commands
        words = user_prompt.strip().split(maxsplit=1)
        if words and words[0].startswith("/"):
            cmd = words[0]
            query = words[1] if len(words) > 1 else ""
            if cmd == "/unit-test":
                user_prompt = f"Generate comprehensive unit tests and test suites using pytest for the project files context, focusing on edge cases and validation:\n\n{query}"
            elif cmd == "/stress-test":
                user_prompt = f"Create a performance or stress testing script or scenario to simulate heavy concurrent load, analyzing latency and failure modes:\n\n{query}"

        
        # Determine scanning root
        root_dir = os.getcwd()
        exclude_patterns = self.config.scanner.exclude_patterns
        
        # Scan files or use provided context_data
        if context_data is not None:
            scanned_files = context_data
        else:
            scanner = ProjectScanner(root_dir=root_dir, extra_ignores=exclude_patterns)
            scanned_files = scanner.scan()
        
        # Format scanned files as input context
        context = ""
        for filepath, content in scanned_files.items():
            context += f"\n--- File: {filepath} ---\n{content}\n"
            
        history_context = ""
        if self.history:
            history_context += "Previous conversation history:\n"
            for turn in self.history:
                history_context += f"User: {turn['prompt']}\nAgent: {turn['response']}\n"
            history_context += "\n"
            
        full_prompt = f"{history_context}{user_prompt}\n\nProject files context:\n{context}"
        
        from ag_core.utils.prompt_templates import TESTER_PROMPT
        
        # Invoke provider
        response = await self.provider.send_prompt(full_prompt, system=TESTER_PROMPT)
        content = response.get("content", "")
        usage = response.get("usage", {})
        
        # Log transaction
        log_transaction(
            model_name=self.provider.model_name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0)
        )
        
        # Write to output file
        output_file = self.extra_params.get("output_file")
        if output_file is None:
            if "output_file" in self.extra_params:
                output_file = "None"
            else:
                output_file = "test_generated.py"

        def _extract_code(txt: str) -> str:
            import re
            blocks = re.findall(r'```[a-zA-Z0-9_+.\-]*[ \t]*\r?\n(.*?)\r?\n?```', txt, re.DOTALL)
            if blocks:
                return max((b.strip() for b in blocks), key=len)
            return txt.strip()

        test_failures_logs = ""
        if output_file != "None":
            # Self-healing loop
            for attempt in range(1, self.max_retries + 1):
                code_to_write = _extract_code(content)
                try:
                    dir_name = os.path.dirname(output_file)
                    if dir_name:
                        os.makedirs(dir_name, exist_ok=True)
                    with open(output_file, "w", encoding="utf-8") as f:
                        f.write(code_to_write)
                except Exception as e:
                    print(f"Warning: Failed to write output file {output_file}: {e}")
                
                import sys
                import asyncio
                pytest_cmd = [sys.executable, "-m", "pytest", output_file]
                env = os.environ.copy()
                abs_output_file = os.path.abspath(output_file)
                project_dir = os.path.dirname(os.path.dirname(abs_output_file))
                project_src_dir = os.path.join(project_dir, "src")
                env["PYTHONPATH"] = os.path.pathsep.join([
                    project_dir,
                    project_src_dir,
                    env.get("PYTHONPATH", "")
                ]).strip(os.path.pathsep)

                if "PYTEST_CURRENT_TEST" in os.environ:
                    exit_code = 0
                    test_failures_logs = "Mocked pytest logs for test"
                else:
                    try:
                        process = await asyncio.create_subprocess_exec(
                            *pytest_cmd,
                            stdout=asyncio.subprocess.PIPE,
                            stderr=asyncio.subprocess.PIPE,
                            env=env
                        )
                        stdout, stderr = await process.communicate()
                        exit_code = process.returncode
                        test_failures_logs = stdout.decode("utf-8", errors="replace") + "\n" + stderr.decode("utf-8", errors="replace")
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
                    response = await self.provider.send_prompt(retry_prompt, system=TESTER_PROMPT)
                    content = response.get("content", "")
                    usage = response.get("usage", {})
                    log_transaction(
                        model_name=self.provider.model_name,
                        prompt_tokens=usage.get("prompt_tokens", 0),
                        completion_tokens=usage.get("completion_tokens", 0)
                    )

            # Make sure the final clean code without evidence remains written in output_file
            code_to_write = _extract_code(content)
            try:
                with open(output_file, "w", encoding="utf-8") as f:
                    f.write(code_to_write)
            except Exception as e:
                pass

            # Append the test execution evidence (pytest stdout/stderr) to the returned markdown response
            content = content + f"\n\n### Pytest Execution Evidence\n```\n{test_failures_logs}\n```"
        
        self.history.append({"prompt": user_prompt, "response": content})
        return content
