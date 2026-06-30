import asyncio
import json
import shutil
from unittest.mock import AsyncMock, patch
import os

from ag_core.providers.grok_provider import GrokProvider


async def run_test():
    mock_process = AsyncMock()
    mock_process.communicate.return_value = (
        json.dumps(
            {
                "result": "Hello from Grok CLI login test!",
                "usage": {"input_tokens": 10, "output_tokens": 5},
            }
        ).encode("utf-8"),
        b"",
    )

    # We patch and print exceptions
    with patch("shutil.which", return_value="/usr/local/bin/grok"), patch.dict(
        "os.environ", {}, clear=True
    ), patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_exec:
        mock_exec.return_value = mock_process

        provider = GrokProvider(api_key=None)

        # Override self.api_key to None explicitly to ensure it enters
        provider.api_key = None

        # We can hook into the send_prompt method to catch exceptions and print
        orig_send_prompt = provider.send_prompt

        # Let's inspect create_subprocess_exec calls
        try:
            response = await provider.send_prompt("Test prompt")
        except Exception as ex:
            print("OUTER EXCEPTION:", repr(ex))

        print("CALL COUNT:", mock_exec.call_count)
        print("CALLS:", mock_exec.call_args_list)


asyncio.run(run_test())
