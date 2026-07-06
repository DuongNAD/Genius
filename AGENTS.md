# Genius — read this before touching anything

This repository is the **Genius multi-agent framework** (Antigravity 2.0
orchestrator backend). On this machine it is an **installed tool**, already
registered as the MCP server `genius` and as the `genius-orchestrator` skill.

**Do NOT modify, refactor, or "fix" code in this repository** unless the user
explicitly says they want to work on the Genius framework itself.

If your task is to build, research, design, review, test, or audit something,
do it by **calling the `genius` MCP tools** (`research`, `design`, `code`,
`unit_test`, `security_audit`, `deploy`, `review`, `debate`, `doctor`,
`code_graph`, `notebooklm_list`, `notebooklm_query`, `notebooklm_research`,
`orchestrate`, `orchestrate_status`, `orchestrate_approve`,
`orchestrate_reject`) and put the results in the user's own workspace — not
here. `code_graph` answers repo-structure questions (where is a symbol
defined, who imports a file, ranked repo map) without running any agent;
`notebooklm_*` query/deep-research a NotebookLM notebook (needs `nlm login`).

To start the API servers needed by `orchestrate`: `py serve.py` (Windows `py`
launcher; port 8080 is taken by another service on this machine, skip the
dashboard).
