---
name: claude_architect
description: Claude Architect Agent scans project files, designs architecture, and documents structure and layout.
---
# Claude Architect Skill

This skill allows the user to invoke the Claude Architect Agent. It takes the research input from the previous step and designs the system architecture, writing the architectural design document.

## CLI Arguments

- `--input`: Path to the input file containing research findings or prompt context (Required).
- `--output`: Path to write the system architecture/design document (Default: `architecture.md`).
