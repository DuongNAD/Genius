# Antigravity `/genius` trigger

Genius exposes MCP **tools** (`genius_*`), not a slash command. To type `/genius`
in the Antigravity app you add a **local Workflow (IDE)** or **Skill (CLI)** — a
short Markdown prompt that tells the agent to call the `genius_*` tools. Files
here provide that content.

## IDE (Antigravity app — Customizations → Workflows)

The IDE stores workflows through its own UI, not a file path.

1. In Antigravity: **Customizations → Workflows → New Workflow** (or the
   `... More Options` menu).
2. Name it `genius`.
3. Paste the body of [`genius.workflow.md`](genius.workflow.md).
4. Save. Invoke in chat with `/genius <your build request>`.

## CLI / TUI (Antigravity CLI)

Antigravity CLI auto-registers skills from `.agents/skills/<name>.md` in the
project you open.

```bash
# from the project you drive with the Antigravity CLI:
mkdir -p .agents/skills
cp /Users/duongnad/Documents/project/Genius/antigravity/genius.skill.md .agents/skills/genius.md
```

Then `/genius <your build request>` is available in that project.

## What it does

Both drive `genius_orchestrate` with `pipeline: "custom"` (plan-first → codex
debate → gemini coding → codex-gpt5.6-sol review), poll `genius_orchestrate_status`,
and report the artifacts. The models come from the `genius` server's env in
`~/.gemini/config/mcp_config.json` (kept in sync with the CLI `.env`).

> Note: a bare `/genius` cannot come from the MCP server itself — the MCP `prompts`
> capability that some clients render as slash commands is not implemented by
> Genius and not confirmed to surface in Antigravity. A local Workflow/Skill is
> the supported way to get a `/genius` command.
