---
name: tester_agent
description: Tester Agent receives Codex's review output, scans project files, and writes automatically generated unit tests/scenarios.
---
# Tester Agent Skill

This skill allows the user to invoke the Tester Agent. It scans the project files and uses Codex's review output to generate unit tests and test scenarios.

## CLI Arguments

- `--review`: Path to the file containing Codex's review output (Required).
- `--output`: Path to write the generated unit tests (Default: `test_generated.py`).
