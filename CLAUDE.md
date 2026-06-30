# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Language rule (from `.agents/AGENTS.md`)

- **Reply to the user in Vietnamese (Tiếng Việt).**
- **All code, comments, internal logs, system prompts, and commit messages must be in English.**

## What this is

Genius is a distributed multi-agent framework: one FastAPI "skill server" process per agent role, coordinated by an async pipeline. Core library lives in `ag_core/` (`agents/`, `providers/`, `interfaces/`, `distributed/`, `memory/`, `scanner/`, `utils/`); root scripts are the entrypoints.

Agent ports: Hub 8000, Grok/Researcher 8001, Claude/Architect 8002, Codex/Reviewer 8003, Tester 8004, Security 8005, DevOps 8006, Dashboard 8080.

## Commands

- Install: `pip install -r requirements.txt`
- Run all tests: `python -m pytest` (exactly what CI runs, no args)
- Single test: `python -m pytest test_e2e.py::TestClass::test_name` or `python -m pytest -k "pattern"`
- Lint: `flake8` — Format: `black .` (both run with **default settings**, no config files)
- Run the stack: `python serve.py` (interactive menu). Flags: `--roles grok,claude,...`, `--distributed`, `--auto-pilot`, `--pipeline e2e|sequential`, `--prompt "..."`, `--hub-port 8000`. Also `python orchestrator.py`, `python dashboard.py`, `python mcp_server.py`, or `docker compose up`.

## Gotchas

- **`black` and `flake8` are pinned in `.pre-commit-config.yaml` only — NOT in `requirements.txt`.** Run `pip install black flake8` (or `pre-commit install`) before formatting/linting. Pre-commit hooks: trailing-whitespace, end-of-file-fixer, check-yaml, check-added-large-files, black 24.4.2, flake8 7.0.0.
- Tests live in **two** places: `test_*.py` in the repo root AND `tests/`. The root `verify_*.py` files are **manual scripts** (run with `python verify_db.py`), not pytest tests. `pytest.ini` sets `norecursedirs = projects .agents`.
- **`config.yaml` is gitignored** but expected to exist at runtime — don't assume it's tracked. Also gitignored: `*.db`, `.env*`, secrets, and pipeline outputs (`research.md`, `design.md`, `app.py`, `review.md`, `architecture.md`).
- **`.agents/*/` subfolders are gitignored**, so `.agents/skills/<agent>/run.py` (referenced by the `claude`/`codex`/`grok`/`tester` wrapper scripts and PROJECT.md) is NOT in the repo and the wrappers fail unless those skills are generated locally.
- **Test/prod security divergence:** production `ag_core/utils/security.py` is HMAC-SHA256 only (no plain-SHA256 fallback). `conftest.py` monkeypatches `verify_checksum`/`verify_raw_body_checksum` to re-allow plain SHA-256 for legacy tests — except `test_upgrades*`. Some tests (`*distributed*`, `*robustness*`, `milestone3_adversarial`) set `SKILL_API_KEY=valid-api-key`; others use `mock-skill-key`.
- Env vars (no `.env.example` exists): `SKILL_API_KEY` (inter-service auth), `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GROK_API_KEY`, `GENIUS_DB_PATH`. Providers are local-CLI-first (Grok shells out to a `grok` CLI; OpenAIProvider parses Codex Desktop JSONL streams) with API keys as fallback.
- Windows-first project (CI runs on `windows-latest`).

## Workflow

- Commit straight to `main` — no PR ceremony.
- **Integrity norm (see `TEST_INFRA.md`):** never hardcode test results or write facade/stub implementations to make tests pass — a "Forensic Auditor" verifies real work.
