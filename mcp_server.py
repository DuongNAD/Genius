import os
import sys
import json
import asyncio
from typing import Any, Dict, List
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

# Add project root to sys.path
root_dir = os.path.dirname(os.path.abspath(__file__))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from ag_core.config import load_config
from ag_core.providers.grok_provider import GrokProvider
from ag_core.providers.anthropic_provider import AnthropicProvider
from ag_core.providers.openai_provider import OpenAIProvider
from ag_core.agents.grok_researcher import GrokResearcherAgent
from ag_core.agents.claude_architect import ClaudeArchitectAgent
from ag_core.agents.codex_reviewer import CodexReviewerAgent
from ag_core.agents.tester import TesterAgent
from ag_core.agents.security_agent import SecurityAgent
from ag_core.agents.devops_agent import DevOpsAgent

app = FastAPI(title="Genius MCP Server")

class CallToolRequest(BaseModel):
    name: str
    arguments: Dict[str, Any]

async def execute_agent(agent_name: str, prompt: str, context: Dict[str, str] = None) -> str:
    config = load_config()
    if agent_name == "research":
        provider = GrokProvider(api_key=config.grok_api_key, model_name=config.models.grok)
        agent = GrokResearcherAgent(provider=provider, config=config, output_file="None")
    elif agent_name == "design":
        provider = AnthropicProvider(api_key=config.anthropic_api_key, model_name=config.models.anthropic)
        agent = ClaudeArchitectAgent(provider=provider, config=config, output_file="None")
    elif agent_name == "code":
        provider = OpenAIProvider(api_key=config.openai_api_key, model_name=config.models.openai)
        agent = CodexReviewerAgent(provider=provider, config=config, output_file="None")
        prompt = f"/code {prompt}"
    elif agent_name == "unit_test":
        provider = OpenAIProvider(api_key=config.openai_api_key, model_name=config.models.openai)
        agent = TesterAgent(provider=provider, config=config, output_file="None")
    elif agent_name == "security_audit":
        provider = OpenAIProvider(api_key=config.openai_api_key, model_name=config.models.openai)
        agent = SecurityAgent(provider=provider, config=config, output_file="None")
    elif agent_name == "deploy":
        provider = AnthropicProvider(api_key=config.anthropic_api_key, model_name=config.models.anthropic)
        agent = DevOpsAgent(provider=provider, config=config, output_file="None")
    else:
        raise ValueError(f"Unknown agent: {agent_name}")
        
    return await agent.run(prompt=prompt, context_data=context)

TOOLS = [
    {
        "name": "research",
        "description": "Perform in-depth requirements research and identify technical challenges.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The research query or topic"},
                "context": {"type": "object", "description": "Optional file context as dict of filepath -> content"}
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "design",
        "description": "Develop high-level software architecture plans and component designs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The system design description or requirements"},
                "context": {"type": "object", "description": "Optional file context"}
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "code",
        "description": "Write or refactor high-quality code implementation based on specifications.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "The coding requirements or specification"},
                "context": {"type": "object", "description": "Optional existing files context"}
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "unit_test",
        "description": "Generate comprehensive test cases and verify implementation behavior.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Code content or test description"},
                "context": {"type": "object", "description": "Optional context"}
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "security_audit",
        "description": "Perform security audit on the code to detect vulnerabilities and secrets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Code content or security concerns to audit"},
                "context": {"type": "object", "description": "Optional context"}
            },
            "required": ["prompt"]
        }
    },
    {
        "name": "deploy",
        "description": "Generate CI/CD configuration, Dockerfiles, and deployment strategies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Deployment requirements"},
                "context": {"type": "object", "description": "Optional context"}
            },
            "required": ["prompt"]
        }
    }
]

@app.get("/tools")
async def list_tools():
    return {"tools": TOOLS}

@app.post("/tools/call")
async def call_tool(req: CallToolRequest):
    valid_tool_names = {t["name"] for t in TOOLS}
    if req.name not in valid_tool_names:
        raise HTTPException(status_code=400, detail=f"Tool {req.name} not found")
        
    prompt = req.arguments.get("prompt", "")
    context = req.arguments.get("context")
    
    try:
        result = await execute_agent(req.name, prompt, context)
        return {"content": [{"type": "text", "text": result}]}
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

async def run_stdio_mcp():
    import logging
    for handler in logging.root.handlers[:]:
        logging.root.removeHandler(handler)
    logging.basicConfig(stream=sys.stderr, level=logging.INFO)
    
    real_stdout = sys.stdout
    sys.stdout = sys.stderr
    
    loop = asyncio.get_running_loop()
    
    if sys.platform != "win32":
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)
        
    while True:
        if sys.platform == "win32":
            line = await loop.run_in_executor(None, sys.stdin.readline)
            if not line:
                break
            line_str = line
        else:
            line_bytes = await reader.readline()
            if not line_bytes:
                break
            line_str = line_bytes.decode('utf-8')
        try:
            req = json.loads(line_str)
            req_id = req.get("id")
            method = req.get("method")
            params = req.get("params", {})
            
            if method == "tools/list":
                res = {"jsonrpc": "2.0", "id": req_id, "result": {"tools": TOOLS}}
            elif method == "tools/call":
                name = params.get("name")
                arguments = params.get("arguments", {})
                prompt = arguments.get("prompt", "")
                context = arguments.get("context")
                try:
                    content = await execute_agent(name, prompt, context)
                    res = {"jsonrpc": "2.0", "id": req_id, "result": {"content": [{"type": "text", "text": content}]}}
                except Exception as e:
                    res = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32000, "message": str(e)}}
            else:
                res = {"jsonrpc": "2.0", "id": req_id, "error": {"code": -32601, "message": "Method not found"}}
                
            real_stdout.write(json.dumps(res) + "\n")
            real_stdout.flush()
        except Exception as e:
            sys.stderr.write(f"Error handling request: {e}\n")
            sys.stderr.flush()

if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "stdio":
        asyncio.run(run_stdio_mcp())
    else:
        import uvicorn
        uvicorn.run("mcp_server:app", host="0.0.0.0", port=8000)
