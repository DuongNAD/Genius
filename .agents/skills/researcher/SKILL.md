---
name: researcher
description: Grok Researcher Agent scans project files, researches requirements, and documents findings.
---
# Grok Researcher Skill

This skill allows the user to invoke the Grok Researcher Agent. It scans the files in the workspace, gathers context, and sends a query to the Grok provider to produce a research report.

## CLI Arguments

- `--query` / `--prompt`: The research query or prompt specifying what information to analyze. (Required)
- `--output`: Path to write the research findings (Default: `research.md`).
