# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

Genius (a.k.a. Antigravity 2.0) is an autonomous multi-agent framework for code generation, refactoring, and automated testing. It runs as a set of microservices: six role-specialized agents, each exposed as an independent FastAPI service, coordinated by an async orchestrator. Everything runs locally — agent "providers" shell out to local CLI tools (`grok`, `claude`, `codex`, etc.) rather than calling hosted APIs directly.

The README and most design docs are written in Vietnamese; respond in Vietnamese when the user writes in Vietnamese.

## Commands

```bash
pip install -r requirements.txt          # install deps (Python 3.10+)

python serve.py                          # interactive startup menu (pick which agents to launch)
python serve.py --roles grok,claude      # launch specific agent API servers
python serve.py --distributed            # start the central hub (WebSocket worker registry, port 8000)
python serve.py --auto-pilot --prompt "..."  # start all servers + run the pipeline

python orchestrator.py --prompt "build a TODO API"   # run the full pipeline directly
python orchestrator.py --prompt "..." --pipeline e2e # E2E pipeline variant
python dashboard.py                      # TUI / WebSocket monitoring dashboard (port 8080)
python mcp_server.py                     # MCP server exposing agents as tools

# Tests
python -m pytest                         # full suite (CI runs exactly this, on windows-latest, Python 3.11)
python -m pytest test_orchestrator.py    # single file
python -m pytest tests/test_distributed.py -k "heartbeat"   # single test by name

# Lint / format (pre-commit: black + flake8)
pre-commit run --all-files
flake8 .
black .
```

## Detailed rules

Topic-specific guidance lives in `.claude/rules/` and loads automatically when Claude works on matching files:

- `architecture.md` — request flow, orchestrator/serve/ag_core layout, ports & roles
- `security.md` — JWT + HMAC inter-service auth model (HMAC-only in production)
- `configuration.md` — `config.yaml`, `.env`, and pytest URL rewriting
- `testing.md` — pytest collection rules, conftest key seeding, pytest-only behavior toggles
