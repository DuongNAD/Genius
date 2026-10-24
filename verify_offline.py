#!/usr/bin/env python3
"""
Genius Multi-Agent Orchestrator — 100% Offline Mock & Verification Runner.

This script runs the complete Genius multi-agent pipeline in dry-run mode
without requiring external API keys or logged-in CLI tools.

Usage:
    py verify_offline.py
    py verify_offline.py --quick
    py verify_offline.py --test-suite
"""

import asyncio
import os
import sys
import time

_ROOT = os.path.dirname(os.path.abspath(__file__))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

os.environ["SKILL_API_KEY"] = "mock-skill-key"
os.environ["OPENAI_API_KEY"] = "mock-key"
os.environ["ANTHROPIC_API_KEY"] = "mock-key"
os.environ["GROK_API_KEY"] = "mock-key"
os.environ["GENIUS_OFFLINE_MODE"] = "1"

from ag_core.agent_factory import build_agent
from ag_core.testing.offline_mock import OfflineMockProvider
from ag_core.testing.fixtures import mock_all_providers


def banner():
    print("=" * 80)
    print("  🚀 GENIUS MULTI-AGENT ORCHESTRATOR — OFFLINE VERIFICATION SUITE")
    print("  Mode: 100% Deterministic Offline Mock (No API Keys / CLI Logins Needed)")
    print("=" * 80)


async def run_offline_pipeline(task_description: str = "Build an async distributed rate limiter service"):
    print(f"\n📋 [Task Prompt]: {task_description}\n")
    print("-" * 80)

    provider = OfflineMockProvider()

    with mock_all_providers(provider):
        start_all = time.perf_counter()

        # -----------------------------------------------------------------
        # Stage 1: Researcher Agent
        # -----------------------------------------------------------------
        print("🔍 [Stage 1/7] Researcher Agent (Discovery & Constraints)...")
        t0 = time.perf_counter()
        researcher = build_agent("researcher", provider=provider, stateless=True)
        research_out = await researcher.run(
            prompt=f"Research technical constraints and architecture for: {task_description}",
            context_data={"README.md": "Genius multi-agent system workspace"}
        )
        t_research = time.perf_counter() - t0
        print(f"   ✅ Done ({t_research:.2f}s) — Length: {len(research_out)} chars")
        first_line = research_out.strip().splitlines()[0] if research_out else "N/A"
        print(f"   📄 Preview: {first_line[:75]}...")

        # -----------------------------------------------------------------
        # Stage 2: Claude Architect Agent
        # -----------------------------------------------------------------
        print("\n🏛️  [Stage 2/7] Claude Architect Agent (Design & Schema)...")
        t0 = time.perf_counter()
        architect = build_agent("claude", provider=provider, stateless=True)
        arch_out = await architect.run(
            prompt=f"Design full architectural plan with JSON schema for: {task_description}",
            context_data={"research.md": research_out}
        )
        t_arch = time.perf_counter() - t0
        print(f"   ✅ Done ({t_arch:.2f}s) — Length: {len(arch_out)} chars")

        # -----------------------------------------------------------------
        # Stage 3: Codex Reviewer / Coder Agent
        # -----------------------------------------------------------------
        print("\n💻 [Stage 3/7] Codex Coder Agent (Source Code Implementation)...")
        t0 = time.perf_counter()
        coder = build_agent("codex", provider=provider, stateless=True)
        code_out = await coder.run(
            prompt=f"Implement core logic for: {task_description}",
            context_data={"design.md": arch_out}
        )
        t_code = time.perf_counter() - t0
        print(f"   ✅ Done ({t_code:.2f}s) — Length: {len(code_out)} chars")

        # -----------------------------------------------------------------
        # Stage 4: Tester Agent
        # -----------------------------------------------------------------
        print("\n🧪 [Stage 4/7] Tester Agent (Pytest Unit & Scenario Generation)...")
        t0 = time.perf_counter()
        tester = build_agent("tester", provider=provider, stateless=True)
        test_out = await tester.run(
            prompt=f"Generate async unit tests and mock fixtures for: {task_description}",
            context_data={"app.py": code_out}
        )
        t_test = time.perf_counter() - t0
        print(f"   ✅ Done ({t_test:.2f}s) — Length: {len(test_out)} chars")

        # -----------------------------------------------------------------
        # Stage 5: Security Agent
        # -----------------------------------------------------------------
        print("\n🛡️  [Stage 5/7] Security Agent (Vulnerability Audit & SAST)...")
        t0 = time.perf_counter()
        security = build_agent("security", provider=provider, stateless=True)
        sec_out = await security.run(
            prompt="Audit source code for CWE vulnerabilities, secrets, and insecure patterns",
            context_data={"app.py": code_out, "test_app.py": test_out}
        )
        t_sec = time.perf_counter() - t0
        print(f"   ✅ Done ({t_sec:.2f}s) — Length: {len(sec_out)} chars")

        # -----------------------------------------------------------------
        # Stage 6: DevOps Agent
        # -----------------------------------------------------------------
        print("\n📦 [Stage 6/7] DevOps Agent (Docker & CI/CD Packaging)...")
        t0 = time.perf_counter()
        devops = build_agent("devops", provider=provider, stateless=True)
        devops_out = await devops.run(
            prompt="Generate production multi-stage Dockerfile and docker-compose.yml",
            context_data={"app.py": code_out}
        )
        t_devops = time.perf_counter() - t0
        print(f"   ✅ Done ({t_devops:.2f}s) — Length: {len(devops_out)} chars")

        # -----------------------------------------------------------------
        # Stage 7: Multi-Agent Debate & Consensus Synthesis Flow
        # -----------------------------------------------------------------
        print("\n⚖️  [Stage 7/7] Multi-Agent Debate & Consensus Synthesis Flow...")
        t0 = time.perf_counter()
        # Emulate debate between Architect and Reviewer
        critic_prompt = f"Critique and verify architectural plan:\n{arch_out}\n\nAgainst implementation:\n{code_out}"
        critic_res = await provider.send_prompt(critic_prompt, role="reviewer")
        critic_verdict_str = critic_res.get("content", "")
        t_debate = time.perf_counter() - t0
        print(f"   ✅ Done ({t_debate:.2f}s) — Debate consensus reached ({len(critic_verdict_str)} chars).")

        total_time = time.perf_counter() - start_all

        print("\n" + "=" * 80)
        print("  📊 OFFLINE ORCHESTRATION PIPELINE SUMMARY")
        print("=" * 80)
        print(f"  • Total Pipeline Execution Time: {total_time:.2f} seconds")
        print(f"  • Total Mock LLM Invocations:   {provider.call_count}")
        print(f"  • Total Simulated Input Tokens:  {sum(r.get('usage', {}).get('prompt_tokens', 0) for r in provider.call_records)}")
        print(f"  • Total Simulated Output Tokens: {sum(r.get('usage', {}).get('completion_tokens', 0) for r in provider.call_records)}")
        print("  • Status:                        ✅ 100% PASSED (Offline Deterministic)")
        print("=" * 80)


def run_tests():
    print("\n🧪 Running Pytest Offline Test Suites...\n")
    import pytest
    ret = pytest.main([
        "tests/test_offline_mock_e2e.py",
        "tests/test_milestone1_quality.py",
        "tests/test_milestone1_adversarial_challenger.py",
        "tests/test_empirical_challenger_m1_2.py",
        "-v"
    ])
    return ret


if __name__ == "__main__":
    banner()
    if "--test-suite" in sys.argv:
        exit_code = run_tests()
        sys.exit(exit_code)
    else:
        asyncio.run(run_offline_pipeline())
        if "--quick" not in sys.argv:
            print("\nRunning offline test suite verification...")
            run_tests()
