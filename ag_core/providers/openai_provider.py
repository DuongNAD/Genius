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
            
            # Locate codex prioritized in PATH, then fallbacks
            import shutil
            cli_path = shutil.which("codex") or shutil.which("codex.exe")
            
            if not cli_path:
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
                    cli_path = "codex" if os.name != "nt" else "codex.exe"

            if sys_prompt:
                prompt = f"{sys_prompt}\n\n{prompt}"

            import sys

            # `codex exec [PROMPT]` treats the positional arg as the literal
            # instructions — it has no flag to read the prompt from a file.
            # Passing a temp-file PATH (the old behaviour) made Codex run with
            # the path string as its instructions. Instead use "-" so Codex
            # reads the prompt from stdin; this also sidesteps the Windows
            # command-line length limit the temp file was trying to avoid.
            cmd = [
                cli_path,
                "exec",
                "-",
                "--dangerously-bypass-approvals-and-sandbox",
                "--json"
            ]

            actual_cmd = cmd
            if sys.platform == "win32":
                resolved_cli = shutil.which(cli_path) or cli_path
                if resolved_cli.lower().endswith((".cmd", ".bat")):
                    actual_cmd = ["cmd.exe", "/c"] + cmd

            prompt_bytes = prompt.encode("utf-8")
            try:
                process = await asyncio.create_subprocess_exec(
                    *actual_cmd,
                    stdin=asyncio.subprocess.PIPE,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE
                )
            except OSError:
                if sys.platform == "win32" and actual_cmd == cmd:
                    actual_cmd = ["cmd.exe", "/c"] + cmd
                    process = await asyncio.create_subprocess_exec(
                        *actual_cmd,
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE
                    )
                else:
                    raise
            stdout, stderr = await process.communicate(input=prompt_bytes)

            if isinstance(process.returncode, int) and process.returncode != 0:
                stderr_str = stderr.decode("utf-8", errors="ignore").strip()
                raise RuntimeError(f"Codex CLI failed with exit code {process.returncode}: {stderr_str}")
            
            stdout_str = stdout.decode("utf-8", errors="ignore")
            
            content_parts = []
            prompt_tokens = 0
            completion_tokens = 0
            total_tokens = 0
            
            lines = stdout_str.splitlines()
            accumulator = []
            
            # Helper to convert values to int safely
            def safe_int(val) -> int | None:
                try:
                    return int(val)
                except (ValueError, TypeError):
                    return None
            
            for line in lines:
                try:
                    line_stripped = line.strip()
                    if not line_stripped:
                        continue

                    # Strip prefix noise if present
                    idx = -1
                    for char in ('{', '['):
                        pos = line_stripped.find(char)
                        if pos != -1 and (idx == -1 or pos < idx):
                            idx = pos
                    if idx != -1:
                        line_stripped = line_stripped[idx:]
                    
                    # Try to parse line directly first
                    data = None
                    try:
                        data = json.loads(line_stripped)
                        accumulator = []  # Clear accumulator on successful parse of a single line
                    except (Exception, RecursionError):
                        # Line itself is not valid JSON, so we append to accumulator
                        accumulator.append(line)
                        if len(accumulator) > 50:
                            accumulator = accumulator[-50:]
                            
                        # Clean leading lines in accumulator that cannot start JSON
                        while accumulator and not (accumulator[0].strip().startswith('{') or accumulator[0].strip().startswith('[')):
                            accumulator.pop(0)
                            
                        # Try to parse from suffix starting points to recover from noise
                        for i in range(len(accumulator)):
                            # Only try suffixes that start with { or [ to avoid parsing nested structures
                            suffix_start = accumulator[i].strip()
                            if not (suffix_start.startswith('{') or suffix_start.startswith('[')):
                                continue
                                
                            # Check prefix for any unmatched open braces to prevent parsing inner objects too early
                            prefix_text = "".join(accumulator[:i])
                            opens = prefix_text.count('{') + prefix_text.count('[')
                            closes = prefix_text.count('}') + prefix_text.count(']')
                            if opens > closes:
                                continue  # Likely nested inside an unmatched outer structure in the prefix
                                
                            try:
                                accumulated_str = "\n".join(accumulator[i:])
                                parsed = json.loads(accumulated_str)
                                accumulator = []  # Clear on success
                                if isinstance(parsed, dict):
                                    data = parsed
                                break
                            except (Exception, RecursionError):
                                pass
                                
                        if data is None:
                            continue
                            
                    if not isinstance(data, dict):
                        continue
                        
                    event_type = data.get("event") or data.get("type")
                    item = data.get("item")
                    if not isinstance(item, dict):
                        item = {}
                        
                    is_agent_msg = False
                    if event_type == "agent_message" or item.get("type") == "agent_message":
                        is_agent_msg = True
                    elif "agent_message" in data:
                        am = data.get("agent_message")
                        if isinstance(am, dict):
                            is_agent_msg = True
                            if am.get("item") and isinstance(am.get("item"), dict):
                                item = am.get("item")
                    elif (
                        event_type == "item.completed"
                        and isinstance(data.get("item"), dict)
                    ):
                        item = data.get("item")
                        if isinstance(item, dict) and (item.get("type") == "agent_message" or item.get("event") == "agent_message"):
                            is_agent_msg = True

                    if is_agent_msg:
                        text = item.get("text") if isinstance(item, dict) else None
                        if text is None:
                            text = data.get("text")
                        if text is None and "agent_message" in data:
                            am = data.get("agent_message")
                            if isinstance(am, dict):
                                text = am.get("text")
                        if text is not None:
                            content_parts.append(str(text))
                                    
                    # Check for turn.completed event or equivalent keys
                    if event_type == "turn.completed" or "turn.completed" in data:
                        # Gather all possible dictionaries containing token counts
                        candidate_dicts = []
                        
                        # Helper to add a dict safely
                        def add_candidate(d):
                            if isinstance(d, dict) and d not in candidate_dicts:
                                candidate_dicts.append(d)
                                
                        add_candidate(data)
                        
                        turn_completed = data.get("turn.completed")
                        if isinstance(turn_completed, dict):
                            add_candidate(turn_completed)
                            add_candidate(turn_completed.get("usage"))
                            add_candidate(turn_completed.get("tokens"))
                            
                        turn_val = data.get("turn")
                        if isinstance(turn_val, dict):
                            completed_val = turn_val.get("completed")
                            if isinstance(completed_val, dict):
                                add_candidate(completed_val)
                                add_candidate(completed_val.get("usage"))
                                add_candidate(completed_val.get("tokens"))
                                
                        add_candidate(data.get("usage"))
                        add_candidate(data.get("tokens"))
                        
                        input_val = None
                        output_val = None
                        total_val = None
                        
                        for d in candidate_dicts:
                            if input_val is None:
                                val = d.get("input_tokens") or d.get("prompt_tokens")
                                parsed = safe_int(val)
                                if parsed is not None:
                                    input_val = parsed
                                    
                            if output_val is None:
                                val = d.get("output_tokens") or d.get("completion_tokens")
                                parsed = safe_int(val)
                                if parsed is not None:
                                    output_val = parsed
                                    
                            if total_val is None:
                                val = d.get("total_tokens") or d.get("total")
                                parsed = safe_int(val)
                                if parsed is not None:
                                    total_val = parsed
                                    
                        if input_val is not None:
                            prompt_tokens = input_val
                        if output_val is not None:
                            completion_tokens = output_val
                        if total_val is not None:
                            total_tokens = total_val
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


