#!/usr/bin/env python3
import argparse
import asyncio
import importlib.util
import os
import sys
import uvicorn

# Add project root to sys.path
root_dir = os.path.dirname(os.path.abspath(__file__))
if root_dir not in sys.path:
    sys.path.insert(0, root_dir)

from orchestrator import run_pipeline

ROUTING_TABLE = {
    "/research": ("grok", 8001),
    "/summarize": ("grok", 8001),
    "/fact-check": ("grok", 8001),
    "/plan": ("claude", 8002),
    "/design": ("claude", 8002),
    "/review-architecture": ("claude", 8002),
    "/code": ("codex", 8003),
    "/refactor": ("codex", 8003),
    "/security": ("security", 8005),
    "/audit": ("security", 8005),
    "/security-audit": ("security", 8005),
    "/unit-test": ("tester", 8004),
    "/stress-test": ("tester", 8004),
    "/deploy": ("devops", 8006),
}

def normalize_roles(roles_str: str) -> list:
    raw_roles = [r.strip().lower() for r in roles_str.split(",") if r.strip()]
    normalized = []
    for r in raw_roles:
        if r in ["1", "grok", "grok_researcher", "grok api", "grok-api"]:
            normalized.append("grok")
        elif r in ["2", "claude", "claude_architect", "claude api", "claude-api"]:
            normalized.append("claude")
        elif r in ["3", "codex", "codex_reviewer", "codex api", "codex-api"]:
            normalized.append("codex")
        elif r in ["4", "tester", "tester_agent", "tester api", "tester-api"]:
            normalized.append("tester")
        elif r in ["5", "orchestrator"]:
            normalized.append("orchestrator")
        elif r in ["6", "dashboard", "web dashboard", "web-dashboard", "dashboard api", "dashboard-api"]:
            normalized.append("dashboard")
        elif r in ["7", "security", "security_agent", "security api", "security-api"]:
            normalized.append("security")
        elif r in ["8", "devops", "devops_agent", "devops api", "devops-api"]:
            normalized.append("devops")
    return normalized

def interactive_prompt() -> list:
    print("=== Antigravity 2.0 Unified Startup Menu ===")
    print("Select specific roles/agents this machine will run (comma-separated):")
    print("1. grok         - Grok Researcher API (Port 8001)")
    print("2. claude       - Claude Architect API (Port 8002)")
    print("3. codex        - Codex Reviewer API (Port 8003)")
    print("4. tester       - Tester Agent API (Port 8004)")
    print("5. orchestrator - Launch Orchestrator Workflow")
    print("6. dashboard    - Web Dashboard (Port 8080)")
    print("7. security     - Security Agent API (Port 8005)")
    print("8. devops       - DevOps Agent API (Port 8006)")
    try:
        choice = input("Enter selection (e.g. 'grok,claude' or '5' or '1,2,3,4,5,6,7,8'): ").strip()
        return normalize_roles(choice)
    except KeyboardInterrupt:
        print("\nExiting.")
        sys.exit(0)

def get_api_app(role: str):
    if role == "grok":
        path = os.path.join(root_dir, ".agents", "skills", "grok_researcher", "api.py")
    elif role == "claude":
        path = os.path.join(root_dir, ".agents", "skills", "claude_architect", "api.py")
    elif role == "codex":
        path = os.path.join(root_dir, ".agents", "skills", "codex_reviewer", "api.py")
    elif role == "tester":
        path = os.path.join(root_dir, ".agents", "skills", "tester_agent", "api.py")
    elif role == "security":
        path = os.path.join(root_dir, ".agents", "skills", "security_agent", "api.py")
    elif role == "devops":
        path = os.path.join(root_dir, ".agents", "skills", "devops_agent", "api.py")
    elif role == "dashboard":
        path = os.path.join(root_dir, "dashboard.py")
    else:
        raise ValueError(f"Unknown role: {role}")
        
    spec = importlib.util.spec_from_file_location(f"{role}_api", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.app

async def start_server(role: str, port: int):
    app = get_api_app(role)
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()

async def main_async():
    parser = argparse.ArgumentParser(description="Unified Startup Menu for Genius Microservices")
    parser.add_argument("--roles", default=None, help="Comma-separated roles to run (grok, claude, codex, tester, orchestrator, dashboard)")
    parser.add_argument("--prompt", default=None, help="Prompt for orchestrator role")
    parser.add_argument("--interactive", action="store_true", help="Interactive design review loop")
    parser.add_argument("--auto-pilot", action="store_true", help="Auto-pilot: start all servers and run pipeline")
    args = parser.parse_args()

    auto_pilot = getattr(args, "auto_pilot", False) is True
    interactive = getattr(args, "interactive", False) is True

    if auto_pilot:
        selected_roles = ["grok", "claude", "codex", "tester", "security", "devops", "dashboard", "orchestrator"]
    elif args.roles:
        selected_roles = normalize_roles(args.roles)
    elif args.prompt is not None:
        selected_roles = ["orchestrator"]
    else:
        selected_roles = interactive_prompt()

    # Dynamic role resolution for prompt command execution
    prompt = args.prompt
    if prompt:
        first_word = prompt.strip().split()[0] if prompt.strip() else ""
        if first_word.startswith("/") and first_word in ROUTING_TABLE:
            target_role, target_port = ROUTING_TABLE[first_word]
            if target_role not in selected_roles:
                selected_roles.append(target_role)
                print(f"Automatically adding agent role '{target_role}' for command routing of '{first_word}'")

    if not selected_roles:
        print("No valid roles selected. Exiting.")
        return

    print(f"Starting selected roles: {selected_roles}")

    server_tasks = []
    # Start requested API servers
    if "grok" in selected_roles:
        server_tasks.append(asyncio.create_task(start_server("grok", 8001)))
    if "claude" in selected_roles:
        server_tasks.append(asyncio.create_task(start_server("claude", 8002)))
    if "codex" in selected_roles:
        server_tasks.append(asyncio.create_task(start_server("codex", 8003)))
    if "tester" in selected_roles:
        server_tasks.append(asyncio.create_task(start_server("tester", 8004)))
    if "security" in selected_roles:
        server_tasks.append(asyncio.create_task(start_server("security", 8005)))
    if "devops" in selected_roles:
        server_tasks.append(asyncio.create_task(start_server("devops", 8006)))
    if "dashboard" in selected_roles:
        server_tasks.append(asyncio.create_task(start_server("dashboard", 8080)))

    # If prompt is provided or orchestrator is explicitly selected
    if "orchestrator" in selected_roles or prompt:
        if server_tasks:
            print("Waiting 1 second for API servers to initialize...")
            await asyncio.sleep(1.0)
            
        if not prompt:
            if auto_pilot:
                print("Error: Prompt is required under auto-pilot mode.")
                for task in server_tasks:
                    task.cancel()
                if server_tasks:
                    await asyncio.gather(*server_tasks, return_exceptions=True)
                return
            try:
                prompt = input("Enter prompt for orchestrator: ").strip()
            except KeyboardInterrupt:
                print("\nExiting.")
                return
                
        if not prompt:
            print("Error: Prompt is required to run the orchestrator.")
            return

        try:
            print(f"Launching orchestrator pipeline with prompt: '{prompt}'")
            if interactive or auto_pilot:
                await run_pipeline(prompt, interactive=interactive)
            else:
                await run_pipeline(prompt)
            print("Orchestrator pipeline completed successfully.")
        except Exception as e:
            print(f"Orchestrator pipeline failed: {e}")

        if args.prompt is not None or auto_pilot:
            for task in server_tasks:
                task.cancel()
            if server_tasks:
                await asyncio.gather(*server_tasks, return_exceptions=True)
            return

            
    # If we started servers, we await them to run continuously
    if server_tasks:
        try:
            print("FastAPI servers are running. Press Ctrl+C to stop.")
            await asyncio.gather(*server_tasks)
        except asyncio.CancelledError:
            print("Servers stopped.")

def main():
    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        print("\nExiting.")

if __name__ == "__main__":
    main()
