---
name: codex_reviewer
description: Codex Reviewer Agent scans project files, performs code review, and reports bugs/vulnerabilities.
---
# Codex Reviewer Skill

This skill allows the user to invoke the Codex Reviewer Agent. It performs an in-depth review of the generated code and provides feedback on design, bugs, style, and security vulnerabilities.

## CLI Arguments

- `--code`: Path to the file containing code to be reviewed (Required).
- `--output`: Path to write the code review summary (Default: `review.md`).
