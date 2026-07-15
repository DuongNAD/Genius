---
name: tester_agent
description: Tester Agent receives Codex's review output, scans project files, and writes automatically generated unit tests/scenarios.
---
# Tester Agent Skill

This skill allows the user to invoke the Tester Agent. It scans the project files and uses Codex's review output to generate unit tests and test scenarios.

## Usage

```bash
python .agents/skills/tester_agent/run.py <prompt...>
```

There are no CLI flags: every argument is joined into a single prompt (e.g.
the code or review output to generate tests for). The agent scans the current
workspace for context itself and writes the generated tests to its default
output file, `test_generated.py`, in the working directory.
