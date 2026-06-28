import os
import glob
import json
import asyncio
from typing import Any, Dict

from ag_core.interfaces.base_provider import BaseProvider, ProviderResponse, TokenUsage

class OpenAIProvider(BaseProvider):
    """
    OpenAI API provider implementation using the local codex CLI.
    """
    def __init__(self, model_name: str = "gpt-4o", api_key: str | None = None, base_url: str | None = None, **kwargs: Any) -> None:
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        base_url = base_url or os.getenv("OPENAI_BASE_URL") or "https://api.openai.com/v1"
        super().__init__(model_name=model_name, api_key=api_key, base_url=base_url, **kwargs)

    async def send_prompt(self, prompt: str, system: str | None = None, **kwargs: Any) -> Dict[str, Any]:
        async with self.semaphore:
            await self.rate_limiter.acquire()
                
            extra = self.extra_params.copy()
            extra.update(kwargs)
            sys_prompt = extra.pop("system", None) or system
            
            # Locate codex.exe prioritized in:
            # - %LocalAppData%\OpenAI\Codex\bin\*\codex.exe
            # - %LocalAppData%\Microsoft\WindowsApps\codex.exe
            # - %ProgramFiles%\WindowsApps\OpenAI.Codex_*\app\resources\codex.exe
            # - Fallback to "codex.exe"
            cli_path = None
            
            localappdata = os.environ.get("LOCALAPPDATA")
            if localappdata:
                pattern1 = os.path.join(localappdata, "OpenAI", "Codex", "bin", "*", "codex.exe")
                matches1 = glob.glob(pattern1)
                if matches1:
                    cli_path = matches1[0]
                    
            if not cli_path and localappdata:
                candidate2 = os.path.join(localappdata, "Microsoft", "WindowsApps", "codex.exe")
                if os.path.exists(candidate2):
                    cli_path = candidate2
                    
            if not cli_path:
                program_files = os.environ.get("ProgramFiles")
                if program_files:
                    pattern3 = os.path.join(program_files, "WindowsApps", "OpenAI.Codex_*", "app", "resources", "codex.exe")
                    matches3 = glob.glob(pattern3)
                    if matches3:
                        cli_path = matches3[0]
                        
            if not cli_path:
                cli_path = "codex.exe"

            if sys_prompt:
                prompt = f"{sys_prompt}\n\n{prompt}"

            cmd = [
                cli_path,
                "exec",
                prompt,
                "--dangerously-bypass-approvals-and-sandbox",
                "--json"
            ]
                
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            if isinstance(process.returncode, int) and process.returncode != 0:
                stderr_str = stderr.decode("utf-8", errors="ignore").strip()
                raise RuntimeError(f"Codex CLI failed with exit code {process.returncode}: {stderr_str}")
            
            stdout_str = stdout.decode("utf-8", errors="ignore")
            
            content_parts = []
            prompt_tokens = 0
            completion_tokens = 0
            total_tokens = 0
            
            lines = stdout_str.splitlines()
            for line in lines:
                try:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        data = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    
                    if not isinstance(data, dict):
                        continue
                    
                    event_type = data.get("event") or data.get("type")
                    
                    is_agent_msg = False
                    item = None
                    if event_type == "agent_message":
                        item = data.get("item")
                        is_agent_msg = True
                    elif "agent_message" in data:
                        am = data.get("agent_message")
                        if isinstance(am, dict):
                            item = am.get("item")
                            is_agent_msg = True
                    elif (
                        event_type == "item.completed"
                        and isinstance(data.get("item"), dict)
                    ):
                        item = data.get("item")
                        if isinstance(item, dict) and item.get("type") == "agent_message":
                            is_agent_msg = True

                    if is_agent_msg and isinstance(item, dict):
                        text = item.get("text")
                        if text is not None:
                            content_parts.append(str(text))
                                    
                    # Check for turn.completed event
                    if event_type == "turn.completed" or "turn.completed" in data:
                        turn_dict = data.get("turn.completed") if isinstance(data.get("turn.completed"), dict) else data
                        if isinstance(turn_dict, dict):
                            input_val = None
                            output_val = None
                            total_val = None
                            
                            # Try to find input tokens
                            for key in ["input_tokens", "prompt_tokens"]:
                                if key in turn_dict:
                                    input_val = turn_dict[key]
                                    break
                            if input_val is None:
                                for sub in ["usage", "tokens"]:
                                    if isinstance(turn_dict.get(sub), dict):
                                        for key in ["input_tokens", "prompt_tokens"]:
                                            if key in turn_dict[sub]:
                                                input_val = turn_dict[sub][key]
                                                break
                                        if input_val is not None:
                                            break
                                            
                            # Try to find output tokens
                            for key in ["output_tokens", "completion_tokens"]:
                                if key in turn_dict:
                                    output_val = turn_dict[key]
                                    break
                            if output_val is None:
                                for sub in ["usage", "tokens"]:
                                    if isinstance(turn_dict.get(sub), dict):
                                        for key in ["output_tokens", "completion_tokens"]:
                                            if key in turn_dict[sub]:
                                                output_val = turn_dict[sub][key]
                                                break
                                        if output_val is not None:
                                            break
                                            
                            # Try to find total tokens
                            for key in ["total_tokens", "tokens"]:
                                if key in turn_dict and not isinstance(turn_dict[key], dict):
                                    total_val = turn_dict[key]
                                    break
                            if total_val is None:
                                for sub in ["usage", "tokens"]:
                                    if isinstance(turn_dict.get(sub), dict):
                                        for key in ["total_tokens", "tokens"]:
                                            if key in turn_dict[sub] and not isinstance(turn_dict[sub][key], dict):
                                                total_val = turn_dict[sub][key]
                                                break
                                        if total_val is not None:
                                            break
                                            
                            if input_val is not None:
                                try:
                                    prompt_tokens = int(input_val)
                                except (ValueError, TypeError):
                                    pass
                            if output_val is not None:
                                try:
                                    completion_tokens = int(output_val)
                                except (ValueError, TypeError):
                                    pass
                            if total_val is not None:
                                try:
                                    total_tokens = int(total_val)
                                except (ValueError, TypeError):
                                    pass
                except Exception:
                    continue
                            
            if total_tokens == 0:
                total_tokens = prompt_tokens + completion_tokens
            
            content = "".join(content_parts)
            
            response = ProviderResponse(
                content=content,
                usage=TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens
                )
            )
            return response.model_dump()


