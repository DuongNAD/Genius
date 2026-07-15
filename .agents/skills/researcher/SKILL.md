---
name: researcher
description: Grok Researcher Agent scans project files, researches requirements, and documents findings.
---
# Grok Researcher Skill

This skill allows the user to invoke the Researcher Agent. It scans the files in the workspace, gathers context, and sends the query through the configured provider chain (default `agy → claude → codex`; the grok backend is opt-in) to produce a research report.

## Usage

```bash
python .agents/skills/researcher/run.py <prompt...>
```

There are no CLI flags: every argument is joined into a single research
prompt. The agent writes its findings to its default output file,
`research.md`, in the working directory.
