"""MCP tool schemas for the Genius server.

Extracted verbatim from ``mcp_server.py`` to keep that module focused on
behaviour. ``mcp_server`` re-imports ``TOOLS`` so ``mcp_server.TOOLS`` and every
existing reference (dispatch, tool listing, SDK server build) keep working. The
canonical tool-name set is still pinned by ``tests/test_realrun_mcp.py``.
"""

TOOLS = [
    {
        "name": "research",
        "description": "Perform in-depth requirements research and identify technical challenges.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The research query or topic",
                },
                "context": {
                    "type": "object",
                    "description": "Optional file context as dict of filepath -> content",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "design",
        "description": "Develop high-level software architecture plans and component designs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The system design description or requirements",
                },
                "context": {"type": "object", "description": "Optional file context"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "code",
        "description": "Write or refactor high-quality code implementation based on specifications.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The coding requirements or specification",
                },
                "context": {
                    "type": "object",
                    "description": "Optional existing files context",
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "unit_test",
        "description": "Generate comprehensive test cases and verify implementation behavior.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Code content or test description",
                },
                "context": {"type": "object", "description": "Optional context"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "security_audit",
        "description": "Perform security audit on the code to detect vulnerabilities and secrets.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "Code content or security concerns to audit",
                },
                "context": {"type": "object", "description": "Optional context"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "deploy",
        "description": "Generate CI/CD configuration, Dockerfiles, and deployment strategies.",
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "description": "Deployment requirements"},
                "context": {"type": "object", "description": "Optional context"},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "orchestrate",
        "description": (
            "Run the FULL multi-agent pipeline (research -> design -> code -> "
            "test + security + deploy) for a build request. Returns a job_id "
            "immediately; poll orchestrate_status to retrieve the artifacts. "
            "Requires the Genius skill servers to be running (python serve.py)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "prompt": {
                    "type": "string",
                    "description": "The build/refactor request to orchestrate",
                },
                "pipeline": {
                    "type": "string",
                    "enum": ["sequential", "e2e", "custom"],
                    "description": (
                        "Pipeline variant (default 'sequential'). 'custom' = the "
                        "plan-first / codex-debate / codex-gpt5.6-sol final-review "
                        "flow (honours GENIUS_REVIEW_ROLE)."
                    ),
                },
                "workspace": {
                    "type": "string",
                    "description": "Optional absolute path where artifacts are written (default: server cwd)",
                },
                "require_approval": {
                    "type": "boolean",
                    "description": (
                        "Pause after each stage (research, design, code; plus "
                        "review + devops on the custom flow) as "
                        "'awaiting_approval' until orchestrate_approve / "
                        "orchestrate_reject is called (sequential and custom "
                        "pipelines; not e2e)"
                    ),
                },
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "orchestrate_approve",
        "description": (
            "Approve the stage a paused orchestrate job is waiting on "
            "(status 'awaiting_approval') so the pipeline continues to the "
            "next stage."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job_id returned by orchestrate",
                }
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "orchestrate_reject",
        "description": (
            "Reject the stage a paused orchestrate job is waiting on: the "
            "pipeline stops and the job is marked failed."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job_id returned by orchestrate",
                },
                "reason": {
                    "type": "string",
                    "description": "Optional reason recorded in the job error",
                },
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "orchestrate_status",
        "description": (
            "Poll the status of a pipeline started by orchestrate. Returns status "
            "(running|completed|failed|awaiting_approval|interrupted), the "
            "workspace directory the files land in, current_stage (what the "
            "pipeline is working on right now — the code stage is the long one), "
            "per-stage progress, and, when completed, the generated artifacts. "
            "Job state survives MCP server restarts: an id from a previous "
            "session is recovered from its on-disk journal (interrupted = the "
            "server restarted mid-run; finished stages' artifacts remain in the "
            "workspace)."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {
                    "type": "string",
                    "description": "The job_id returned by orchestrate",
                }
            },
            "required": ["job_id"],
        },
    },
    {
        "name": "doctor",
        "description": (
            "Preflight readiness check for Genius. Verifies the local vendor "
            "CLIs (agy/claude/codex, grok optional), SKILL_API_KEY, and the "
            "per-role provider fallback chains, and returns a text report "
            "ending in READY or NOT READY. Call this FIRST to check whether "
            "Genius is ready before calling orchestrate. Read-only, no side "
            "effects."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "debate",
        "description": (
            "Adversarially refine a draft design: a researcher-role critic "
            "(agy/Gemini by default) reviews it and a Claude architect "
            "refines it, for up to 'rounds' exchanges "
            "(early exit when the critic replies [APPROVED]). Runs in-process "
            "(no skill servers needed). Returns JSON with the refined design, "
            "an 'approved' flag, and a per-round summary."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "design": {
                    "type": "string",
                    "description": "The draft design/plan to critique and refine",
                },
                "prompt": {
                    "type": "string",
                    "description": "Optional original requirements for context",
                },
                "rounds": {
                    "type": "integer",
                    "minimum": 1,
                    "maximum": 3,
                    "description": "Max critique/refine rounds (default 1, max 3)",
                },
            },
            "required": ["design"],
        },
    },
    {
        "name": "review",
        "description": (
            "Code review of the given code by the Codex reviewer agent, "
            "in-process. Returns the review text; never writes files."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "The code to review"},
                "instructions": {
                    "type": "string",
                    "description": "Optional focus areas or review instructions",
                },
            },
            "required": ["code"],
        },
    },
    {
        "name": "code_graph",
        "description": (
            "Query the workspace's code graph in-process (CodexGraph-style, "
            "read-only, no graph DB): where a symbol is defined or "
            "referenced, what a file imports / what imports it, a file's "
            "signature skeleton, or an aider-style ranked repo map under a "
            "token budget. Python is parsed with stdlib ast; JS/TS/Go via "
            "tree-sitter when installed. Returns JSON."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": [
                        "map",
                        "definition",
                        "references",
                        "importers",
                        "imports",
                        "skeleton",
                    ],
                    "description": "Query type (default: map)",
                },
                "workspace": {
                    "type": "string",
                    "description": "Workspace directory to index (default: server cwd)",
                },
                "symbol": {
                    "type": "string",
                    "description": "Symbol name (required for definition/references)",
                },
                "file": {
                    "type": "string",
                    "description": (
                        "Repo-relative file path (required for "
                        "importers/imports/skeleton)"
                    ),
                },
                "task": {
                    "type": "string",
                    "description": "Optional task text to personalize op=map ranking",
                },
                "budget": {
                    "type": "integer",
                    "description": "Token budget for op=map (default 32000)",
                },
            },
            "required": [],
        },
    },
    {
        "name": "eval",
        "description": (
            "Grade a finished pipeline workspace against eval metrics "
            "(R5 eval flywheel), in-process and read-only (JSON out, never "
            "writes files). op=grade scores a workspace's artifacts/traces; "
            "op=compare diffs two grades and flags regressions; "
            "op=list_metrics lists the built-in metrics. grade defaults to "
            "the deterministic metrics (artifacts_present, design_wellformed, "
            "code_syntax_valid) which run offline; LLM-as-judge metrics "
            "(task_success, grounding, design_quality, final_response_quality) "
            "are opt-in via 'metrics' and call a provider."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "op": {
                    "type": "string",
                    "enum": ["grade", "compare", "list_metrics"],
                    "description": "Operation (default: grade)",
                },
                "workspace": {
                    "type": "string",
                    "description": (
                        "Workspace to grade for op=grade (default: server cwd)"
                    ),
                },
                "metrics": {
                    "type": "string",
                    "description": (
                        "op=grade: comma-separated metric names "
                        "(default: the deterministic set). Use op=list_metrics "
                        "to see all names."
                    ),
                },
                "prompt": {
                    "type": "string",
                    "description": (
                        "op=grade: the original user request, used by the "
                        "task_success/grounding judge metrics"
                    ),
                },
                "baseline": {
                    "type": "object",
                    "description": "op=compare: the baseline grade result",
                },
                "current": {
                    "type": "object",
                    "description": "op=compare: the current grade result",
                },
            },
            "required": [],
        },
    },
    {
        "name": "notebooklm_list",
        "description": (
            "List the NotebookLM notebooks on the authenticated account "
            "(id + title + source_count), via the local `nlm` CLI. Read-only. "
            "Use it to discover a notebook id to pass to notebooklm_query. "
            "Requires a one-time `nlm login` and GENIUS_NLM_PATH set."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "notebooklm_query",
        "description": (
            "Ask a question against an EXISTING NotebookLM notebook and get a "
            "grounded, cited answer (the model answers only from that "
            "notebook's sources). Returns JSON with 'answer', 'citations' and "
            "'references'. Read-only; needs `nlm login`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "notebook": {
                    "type": "string",
                    "description": "Notebook id or alias (see notebooklm_list)",
                },
                "query": {
                    "type": "string",
                    "description": "The question to ask the notebook's sources",
                },
                "source_ids": {
                    "type": "string",
                    "description": "Optional comma-separated source ids to restrict to",
                },
                "conversation_id": {
                    "type": "string",
                    "description": "Optional conversation id for follow-up questions",
                },
            },
            "required": ["notebook", "query"],
        },
    },
    {
        "name": "notebooklm_research",
        "description": (
            "Deep-research a topic with NotebookLM and answer from the sources "
            "it finds. MUTATES: discovers web/Drive sources, imports them into "
            "a notebook (a new one unless 'notebook' is given), then queries "
            "it. Returns JSON with 'notebook_id' and a cited 'answer'. Runs "
            "synchronously - mode 'fast' ~30s, 'deep' ~5min (raise "
            "GENIUS_NLM_RESEARCH_TIMEOUT for deep). Needs `nlm login`."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "The research topic to search for",
                },
                "mode": {
                    "type": "string",
                    "enum": ["fast", "deep"],
                    "description": "fast (~30s, ~10 sources) or deep (~5min, ~40, web only); default fast",
                },
                "source": {
                    "type": "string",
                    "enum": ["web", "drive"],
                    "description": "Where to search for new sources (default web)",
                },
                "notebook": {
                    "type": "string",
                    "description": "Optional existing notebook id to enrich (default: create a new one)",
                },
                "title": {
                    "type": "string",
                    "description": "Optional title for the new notebook (when none is given)",
                },
                "question": {
                    "type": "string",
                    "description": "Optional final question to ask (defaults to the research query)",
                },
            },
            "required": ["query"],
        },
    },
]
