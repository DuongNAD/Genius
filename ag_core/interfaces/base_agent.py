import abc
import asyncio
import os
from typing import Any, List, Dict
from ag_core.interfaces.base_provider import BaseProvider
from ag_core.memory.vector_store import VectorMemory
from ag_core.utils.logger import log_transaction
from ag_core.directives import parse_directives, PromptDirectives


class BaseAgent(abc.ABC):
    """
    Abstract Base Class for all agents.
    Defines the core interface that every agent must implement to run its loop.
    """

    def __init__(self, name: str, provider: BaseProvider, **kwargs: Any) -> None:
        self.name = name
        self.provider = provider
        self.extra_params = kwargs
        # Per-run @modifier state, (re)set by _resolve_user_prompt each call.
        # Safe as instance state only because skill/worker/MCP build a fresh
        # agent per request (stateless bundle); never reuse an agent across
        # concurrent requests or directives would bleed.
        self.directives = PromptDirectives()

        # Read from config memory section if available
        config = kwargs.get("config") or getattr(self, "config", None)
        if not config:
            try:
                from ag_core.config import load_config

                config = load_config()
            except Exception:
                config = None

        memory_enabled = True
        use_chroma = False
        db_path = None
        chroma_persist_dir = None

        if config and hasattr(config, "memory"):
            memory_enabled = config.memory.enabled
            use_chroma = config.memory.use_chroma
            db_path = config.memory.db_path
            chroma_persist_dir = config.memory.chroma_persist_dir

        # Allow kwargs to override config values
        use_memory = kwargs.get("use_memory", memory_enabled)
        use_chroma = kwargs.get("use_chroma", use_chroma)
        db_path = kwargs.get("db_path", db_path)
        chroma_persist_dir = kwargs.get("chroma_persist_dir", chroma_persist_dir)

        self.memory = None
        if use_memory:
            self.memory = VectorMemory(
                collection_name=self.name,
                use_chroma=use_chroma,
                db_path=db_path,
                chroma_persist_dir=chroma_persist_dir,
            )

        self._git = None
        self.history: List[Dict[str, str]] = []

    @property
    def git(self):
        """GitManager, built on first access: no agent touches it on the
        request path, and eager construction cost a load_config() per
        instantiation (i.e. per /run on the skill servers)."""
        if self._git is None:
            from ag_core.utils.git import GitManager

            self._git = GitManager()
        return self._git

    def clear_history(self) -> None:
        self.history.clear()

    def store_memory(self, text: str, metadata: dict | None = None) -> None:
        if self.memory:
            self.memory.add(text=text, metadata=metadata)

    def retrieve_memory(self, query: str, limit: int = 5) -> List[Dict[str, Any]]:
        if self.memory:
            return self.memory.query(query_text=query, n_results=limit)
        return []

    async def store_memory_async(self, text: str, metadata: dict | None = None) -> None:
        """store_memory with the SQLite/embedding work off the event loop."""
        if self.memory:
            await asyncio.to_thread(self.memory.add, text=text, metadata=metadata)

    async def retrieve_memory_async(
        self, query: str, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """retrieve_memory with the O(rows) embedding decode off the event loop."""
        if self.memory:
            return await asyncio.to_thread(
                self.memory.query, query_text=query, n_results=limit
            )
        return []

    def scan_context(self, context_data: dict | None = None):
        """Resolve the project file map (scanning cwd unless context_data is
        supplied) and render it as a prompt context block.

        Returns (scanned_files, context_str).
        """
        if context_data is not None:
            scanned_files = context_data
        else:
            from ag_core.scanner.project_scanner import ProjectScanner

            config = getattr(self, "config", None)
            exclude_patterns = config.scanner.exclude_patterns if config else None
            scanner = ProjectScanner(
                root_dir=os.getcwd(), extra_ignores=exclude_patterns
            )
            scanned_files = scanner.scan()

        context = ""
        for filepath, file_content in scanned_files.items():
            context += f"\n--- File: {filepath} ---\n{file_content}\n"
        return scanned_files, context

    async def scan_context_async(self, context_data: dict | None = None):
        """scan_context with the full-tree disk read off the event loop.

        Every agent's async run() calls this when no context was supplied;
        running the scan inline would stall the skill server's loop (and
        every co-hosted role's /status polls) for the whole walk. With
        explicit context_data there is no disk work — call straight through.
        """
        if context_data is not None:
            return self.scan_context(context_data)
        return await asyncio.to_thread(self.scan_context, None)

    def format_history(self) -> str:
        """Render self.history as a prompt preamble (empty string if none)."""
        if not self.history:
            return ""
        parts = ["Previous conversation history:\n"]
        for turn in self.history:
            parts.append(f"User: {turn['prompt']}\nAgent: {turn['response']}\n")
        parts.append("\n")
        return "".join(parts)

    def resolve_output_file(self, default: str) -> str:
        """Resolve the destination path: an explicit `output_file=None` kwarg
        means 'do not write' (sentinel "None"); absence falls back to default."""
        output_file = self.extra_params.get("output_file")
        if output_file is None:
            if "output_file" in self.extra_params:
                return "None"
            return default
        return output_file

    def write_output(self, output_file: str, content: str) -> None:
        """Write content to output_file, creating parent dirs. The "None"
        sentinel and empty paths are skipped. Write failures are non-fatal."""
        if not output_file or output_file == "None":
            return
        try:
            dir_name = os.path.dirname(output_file)
            if dir_name:
                os.makedirs(dir_name, exist_ok=True)
            with open(output_file, "w", encoding="utf-8") as f:
                f.write(content)
        except Exception as e:
            print(f"Warning: Failed to write output file {output_file}: {e}")

    # ------------------------------------------------------------------
    # Standard run flow. The six role agents share one request shape:
    # resolve the task text, rewrite a leading slash command, scan the
    # workspace, optionally pull vector-memory context, compose the prompt,
    # call the provider, then record history/memory, log usage and write
    # the artifact. Concrete agents set the class knobs below and delegate
    # run() to _run_standard(); agents with extra machinery (codex's
    # verification loop, tester's self-heal loop) reuse the helpers and
    # keep their own run(). Prompt composition here must stay byte-identical
    # to the pre-refactor per-agent code — tests pin the slash rewrites and
    # the memory-context block.
    # ------------------------------------------------------------------

    DEFAULT_TASK = ""
    SLASH_PREFIXES: Dict[str, str] = {}
    SYSTEM_PROMPT = ""
    USES_MEMORY = False
    DEFAULT_OUTPUT_FILE = "output.md"
    # @modifier names this agent accepts (see ag_core.directives). Default: none,
    # so a modifier never reaches an agent that hasn't opted in. Structure-
    # sensitive agents (codex/tester/security/architect) accept only "deep"
    # (effort) so format/variants can't perturb their code/JSON output.
    ACCEPTED_MODIFIERS: frozenset = frozenset()

    def _resolve_user_prompt(self, prompt: str | None) -> str:
        raw = prompt or self.extra_params.get("prompt") or self.DEFAULT_TASK
        cleaned, directives = parse_directives(raw)
        # Gate here, before any guidance/effort is applied, so a rejected
        # modifier is stripped from BOTH the text and the directive object.
        self.directives = self._filter_directives(directives)
        # A directive-only prompt ("@deep") cleans to "" -> fall back to the
        # agent's default task, preserving the old empty-prompt behaviour.
        return cleaned or self.DEFAULT_TASK

    def _filter_directives(self, d: PromptDirectives) -> PromptDirectives:
        """Keep only modifiers this agent's ACCEPTED_MODIFIERS allows; move the
        rest into ``rejected`` (telemetry). Empty directives pass through so the
        common no-modifier path allocates nothing new."""
        if d.is_empty():
            return d
        acc = self.ACCEPTED_MODIFIERS
        rejected = list(d.rejected)

        def gate(name, present):
            if present and name not in acc:
                rejected.append(name)
                return False
            return present

        effort = d.effort if gate("deep", d.effort is not None) else None
        critic = gate("critic", d.critic)
        variants = d.variants if gate("variants", d.variants is not None) else None
        ideas = d.ideas if gate("ideas", d.ideas is not None) else None
        formats = frozenset(f for f in d.formats if f in acc)
        rejected.extend(f for f in d.formats if f not in acc)
        return PromptDirectives(
            effort=effort,
            critic=critic,
            variants=variants,
            ideas=ideas,
            formats=formats,
            raw=d.raw,
            rejected=tuple(rejected),
        )

    # Guidance text per modifier. Effort (@deep) is absent — it goes to the
    # provider, not the prompt. @tight/@redpen act on the CURRENT request's
    # inlined text (skill/worker/MCP are stateless — there is no prior answer).
    _MODIFIER_GUIDANCE = {
        "critic": (
            "Draft an answer, then adversarially critique your own draft for the"
            " strongest objections, then output only the improved final answer."
        ),
        "simple": "Explain in simple words with concrete examples.",
        "table": "Present the answer as a Markdown table.",
        "steps": "Present the answer as a numbered, step-by-step list.",
        "tight": (
            "Tighten the text provided in this request: cut filler, keep the"
            " important information."
        ),
        "natural": "Write in a natural, human style.",
        "redpen": (
            "Act as a line editor and red-pen the text provided in this request:"
            " fix grammar, clarity, and phrasing."
        ),
    }

    def _directive_guidance(self) -> str:
        """Render the accepted directives into a trailing guidance block, or ""
        when there is nothing to say (the byte-identical no-op path)."""
        d = self.directives
        if d.is_empty():
            return ""
        parts = []
        if d.critic:
            parts.append(self._MODIFIER_GUIDANCE["critic"])
        if d.variants:
            parts.append(
                f"Produce {d.variants} genuinely distinct alternative answers,"
                f" each clearly labelled 'Variant 1'..'Variant {d.variants}'."
            )
        if d.ideas:
            parts.append(
                f"Brainstorm {d.ideas} distinct ideas as a bulleted list."
            )
        for name in ("simple", "table", "steps", "tight", "natural", "redpen"):
            if name in d.formats:
                parts.append(self._MODIFIER_GUIDANCE[name])
        if not parts:
            return ""
        return "\n\n--- Response directives ---\n" + "\n".join(
            f"- {p}" for p in parts
        )

    def _route_slash_command(self, user_prompt: str) -> tuple:
        """(rewritten_prompt, cmd) — cmd is the leading "/token" if present
        (known or not, for callers like codex that branch on it); only
        commands listed in SLASH_PREFIXES are rewritten to prefix + query."""
        words = user_prompt.strip().split(maxsplit=1)
        if not words or not words[0].startswith("/"):
            return user_prompt, None
        cmd = words[0]
        query = words[1] if len(words) > 1 else ""
        prefix = self.SLASH_PREFIXES.get(cmd)
        if prefix is None:
            return user_prompt, cmd
        return prefix + query, cmd

    async def _memory_context_block(self, user_prompt: str) -> str:
        past_memories = await self.retrieve_memory_async(user_prompt, limit=3)
        if not past_memories:
            return ""
        block = "\n--- Relevant Historical Memory Context ---\n"
        for i, mem in enumerate(past_memories, 1):
            block += f"Interaction #{i}:\n{mem['text']}\n"
        return block

    def _compose_full_prompt(
        self, user_prompt: str, memory_context: str, context: str
    ) -> str:
        full_prompt = f"{self.format_history()}{user_prompt}\n"
        guidance = self._directive_guidance()
        if guidance:
            full_prompt += f"{guidance}\n"
        if memory_context:
            full_prompt += f"{memory_context}\n"
        full_prompt += f"\nProject files context:\n{context}"
        return full_prompt

    def _log_usage(self, usage: dict) -> None:
        log_transaction(
            model_name=self.provider.model_name,
            prompt_tokens=usage.get("prompt_tokens", 0),
            completion_tokens=usage.get("completion_tokens", 0),
        )

    async def _store_run_memory(self, user_prompt: str, content: str) -> None:
        await self.store_memory_async(
            text=f"Prompt: {user_prompt}\nResponse: {content}",
            metadata={
                "type": "agent_run",
                "task_id": self.extra_params.get("task_id", "unknown"),
            },
        )

    async def _run_standard(
        self,
        prompt: str | None = None,
        context_data: dict | None = None,
        *,
        effort: str | None = None,
    ) -> str:
        user_prompt, _ = self._route_slash_command(self._resolve_user_prompt(prompt))
        _, context = await self.scan_context_async(context_data)
        memory_context = ""
        if self.USES_MEMORY:
            memory_context = await self._memory_context_block(user_prompt)
        full_prompt = self._compose_full_prompt(user_prompt, memory_context, context)

        # An explicit run(effort=...) wins over the @deep-derived value. Passed
        # keyword-only down the call stack (never via env / instance state) so
        # concurrent jobs can't interfere.
        response = await self.provider.send_prompt(
            full_prompt,
            system=self.SYSTEM_PROMPT,
            effort=effort or self.directives.effort,
        )
        content = response.get("content", "")
        usage = response.get("usage", {})

        self.history.append({"prompt": user_prompt, "response": content})
        if self.USES_MEMORY:
            await self._store_run_memory(user_prompt, content)
        self._log_usage(usage)
        self.write_output(self.resolve_output_file(self.DEFAULT_OUTPUT_FILE), content)
        return content

    @abc.abstractmethod
    async def run(self) -> str:
        """
        Executes the agent's logic/loop and returns the final result as a string.
        """
