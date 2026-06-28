import os
import shutil
import json
import asyncio
from typing import Any, Dict

from ag_core.interfaces.base_provider import BaseProvider, ProviderResponse, TokenUsage

class GrokProvider(BaseProvider):
    """
    Grok API (xAI) provider implementation using the local claude CLI.
    """
    def __init__(self, model_name: str = "grok-2-1212", api_key: str | None = None, base_url: str | None = None, **kwargs: Any) -> None:
        api_key = api_key or os.getenv("GROK_API_KEY") or os.getenv("XAI_API_KEY")
        base_url = base_url or os.getenv("GROK_BASE_URL") or "https://api.x.ai/v1"
        super().__init__(model_name=model_name, api_key=api_key, base_url=base_url, **kwargs)

    async def send_prompt(self, prompt: str, system: str | None = None, **kwargs: Any) -> Dict[str, Any]:
        async with self.semaphore:
            await self.rate_limiter.acquire()
                
            extra = self.extra_params.copy()
            extra.update(kwargs)
            sys_prompt = extra.pop("system", None) or system
            
            cli_path = shutil.which("claude")
            if not cli_path:
                appdata = os.getenv("APPDATA")
                if appdata:
                    fallback = os.path.join(appdata, "npm", "claude.cmd")
                    if os.path.exists(fallback):
                        cli_path = fallback
            if not cli_path:
                userprofile = os.getenv("USERPROFILE")
                if userprofile:
                    fallback = os.path.join(userprofile, "AppData", "Roaming", "npm", "claude.cmd")
                    if os.path.exists(fallback):
                        cli_path = fallback
            if not cli_path:
                cli_path = "claude"

            cmd = [cli_path, "-p", prompt, "--bare", "--tools", '""', "--output-format", "json"]
            if sys_prompt:
                cmd.extend(["--system-prompt", sys_prompt])
                
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            stdout, stderr = await process.communicate()
            
            stdout_str = stdout.decode("utf-8", errors="ignore").strip()
            try:
                res_json = json.loads(stdout_str)
            except json.JSONDecodeError:
                res_json = {}
                
            content = res_json.get("result", "")
            prompt_tokens = res_json.get("usage", {}).get("input_tokens", 0)
            completion_tokens = res_json.get("usage", {}).get("output_tokens", 0)
            total_tokens = prompt_tokens + completion_tokens
            
            response = ProviderResponse(
                content=content,
                usage=TokenUsage(
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens
                )
            )
            return response.model_dump()

