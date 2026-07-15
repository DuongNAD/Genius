---
name: claude_architect
description: Claude Architect Agent scans project files, designs architecture, and documents structure and layout.
---
# Claude Architect Skill

This skill allows the user to invoke the Claude Architect Agent. It takes the research input from the previous step and designs the system architecture, writing the architectural design document.

## Usage

```bash
python .agents/skills/claude_architect/run.py <prompt...>
```

There are no CLI flags: every argument is joined into a single prompt. The
agent scans the current workspace for context itself and writes the design
document to its default output file, `design.md`, in the working directory.
