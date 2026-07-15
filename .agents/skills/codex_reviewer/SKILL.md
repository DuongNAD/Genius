---
name: codex_reviewer
description: Codex Reviewer Agent scans project files, performs code review, and reports bugs/vulnerabilities.
---
# Codex Reviewer Skill

This skill allows the user to invoke the Codex Reviewer Agent. It performs an in-depth review of the generated code and provides feedback on design, bugs, style, and security vulnerabilities.

## Usage

```bash
python .agents/skills/codex_reviewer/run.py <prompt...>
```

There are no CLI flags: every argument is joined into a single prompt (e.g.
`/review <code or instructions>`). The agent scans the current workspace for
context itself and writes its report to its default output file, `review.md`,
in the working directory.
