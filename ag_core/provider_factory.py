"""Central provider selection + runtime fallback chains.

The three provider construction sites (``ag_core.skill_app.build_agent``,
``mcp_server.execute_agent`` and ``ag_core.distributed.worker.execute_task``)
all route through :func:`make_provider` so the role -> backend mapping lives in
one place and can be redirected without code changes:

* No env knobs set - every role gets its :data:`DEFAULT_CHAINS` fallback
  chain (a :class:`FallbackProvider`): a backend that dies at runtime is
  retried on the next backend. The default chains do NOT include the grok
  or notebooklm backends (both are opt-in only: grok's account is out of
  credits; NotebookLM answers only from a curated notebook's sources so it
  is a deliberate, per-role choice rather than a general LLM fallback).
* ``GENIUS_PROVIDER_<ROLE>`` (e.g. ``GENIUS_PROVIDER_RESEARCHER=grok,claude``)
  - explicit comma-separated backend chain for one role; overrides everything,
  including bringing the grok backend back. The researcher role also honors
  the legacy ``GENIUS_PROVIDER_GROK`` spelling (the role's old id).
* ``GENIUS_PROVIDER_FALLBACK`` - DEPRECATED no-op, accepted for backward
  compatibility only. Fallback chains are now always the default; setting
  this variable (truthy or falsy) changes nothing.

Blank env values are treated as unset (``os.environ.get(...) or`` - this repo
was bitten before by blank vars shipped in ``.env.example``).
"""

import asyncio
import importlib
import logging
import os
from typing import Any, Callable, Dict, List, Optional, Tuple

logger = logging.getLogger("ag_core")

# backend name -> (provider module, provider class,
#                  config.models attr, config api-key attr or None for
#                  keyless backends)
BACKENDS = {
    "grok": (
        "ag_core.providers.grok_provider",
        "GrokProvider",
        "grok",
        "grok_api_key",
    ),
    "claude": (
        "ag_core.providers.anthropic_provider",
        "AnthropicProvider",
        "anthropic",
        "anthropic_api_key",
    ),
    "codex": (
        "ag_core.providers.openai_provider",
        "OpenAIProvider",
        "openai",
        "openai_api_key",
    ),
    # Antigravity 2.0 (Gemini) via the local agy CLI. Keyless: auth is shared
    # with the Antigravity IDE login.
    "agy": (
        "ag_core.providers.agy_provider",
        "AgyProvider",
        "agy",
        None,
    ),
    # NotebookLM via the local `nlm` CLI. Keyless: auth is a one-time
    # `nlm login`. Opt-in only (like grok): no DEFAULT_CHAINS entry, so it is
    # never invoked unless GENIUS_PROVIDER_<ROLE> names it. Its `models`
    # attribute holds the default notebook id/alias, not an LLM model name.
    "notebooklm": (
        "ag_core.providers.notebooklm_provider",
        "NotebookLMProvider",
        "notebooklm",
        None,
    ),
}

# Legacy role ids accepted at every entry point; canonical id on the right.
# The Researcher ROLE was renamed "grok" -> "researcher" (the grok BACKEND
# keeps its name in BACKENDS above). Old env vars, --roles values, registry
# keys and worker registrations keep working through this map.
ROLE_ALIASES = {"grok": "researcher", "grok_researcher": "researcher"}


def canonical_role(role: str) -> str:
    """Normalize a role id: lower-case, strip, resolve legacy aliases."""
    role = (role or "").strip().lower()
    return ROLE_ALIASES.get(role, role)


# role -> default backend order (the no-env default for every role). The grok
# backend is deliberately absent: it stays registered in BACKENDS for explicit
# opt-in via GENIUS_PROVIDER_<ROLE>, but no default chain ever invokes it.
DEFAULT_CHAINS = {
    "researcher": ["agy", "claude", "codex"],  # Antigravity/Gemini first
    "claude": ["claude", "agy", "codex"],  # Architect
    "codex": ["codex", "claude", "agy"],
    "tester": ["codex", "claude", "agy"],
    "security": ["codex", "claude", "agy"],
    "devops": ["codex", "claude", "agy"],
}

# Env vars consulted per canonical role, in precedence order. Every role reads
# GENIUS_PROVIDER_<ROLE>; the researcher role also honors the legacy
# GENIUS_PROVIDER_GROK spelling (from when the role id was "grok").
_ROLE_CHAIN_ENVS = {
    "researcher": ["GENIUS_PROVIDER_RESEARCHER", "GENIUS_PROVIDER_GROK"],
}


def _explicit_chain_env(role: str) -> Optional[Tuple[str, str]]:
    """The (env var name, raw value) of the explicit chain override for
    ``role`` (canonical), or None when unset/blank."""
    names = _ROLE_CHAIN_ENVS.get(role, [f"GENIUS_PROVIDER_{role.upper()}"])
    for name in names:
        raw = (os.environ.get(name) or "").strip()
        if raw:
            return name, raw
    return None


def resolve_chain(role: str) -> List[str]:
    """Resolve the ordered backend chain for ``role``.

    Precedence: explicit ``GENIUS_PROVIDER_<ROLE>`` > the role's
    :data:`DEFAULT_CHAINS` entry. ``GENIUS_PROVIDER_FALLBACK`` is a deprecated
    no-op and never consulted. Raises :class:`ValueError` for unknown roles or
    unknown backend names in the explicit env value.
    """
    role = canonical_role(role)
    if role not in DEFAULT_CHAINS:
        raise ValueError(
            f"Unknown role: {role!r}; expected one of {sorted(DEFAULT_CHAINS)}"
        )

    explicit = _explicit_chain_env(role)
    if explicit:
        env_name, raw = explicit
        chain = [name.strip().lower() for name in raw.split(",") if name.strip()]
        unknown = sorted(set(chain) - set(BACKENDS))
        if not chain or unknown:
            raise ValueError(
                f"{env_name}={raw!r} names unknown "
                f"backend(s) {unknown or ['<empty>']}; valid backends: "
                f"{', '.join(sorted(BACKENDS))} (comma-separated, in fallback "
                "order)"
            )
        return chain

    return list(DEFAULT_CHAINS[role])


def chain_source(role: str) -> Optional[str]:
    """Which knob produced the chain for ``role`` (for diagnostics output).

    Returns the env var name (``GENIUS_PROVIDER_<ROLE>``, or the legacy
    ``GENIUS_PROVIDER_GROK`` for the researcher role) for an explicit
    override, or ``None`` for the built-in default chain
    (``GENIUS_PROVIDER_FALLBACK`` is a deprecated no-op and never reported).
    """
    explicit = _explicit_chain_env(canonical_role(role))
    if explicit:
        return explicit[0]
    return None


def build_backend(backend: str, config, role: Optional[str] = None):
    """Instantiate the provider for one backend from ``config``.

    Model precedence: ``GENIUS_MODEL_<BACKEND>`` env (blank = unset) >
    ``config.models.<backend>``. An empty final value means "the CLI's own
    default model" — every provider only passes a model flag when non-empty.

    ``role`` (the canonical role this provider serves) is forwarded to the
    provider as an ``extra_params`` entry so a backend can resolve per-role
    knobs — e.g. the claude backend reads ``GENIUS_CLAUDE_EFFORT_<ROLE>`` so
    the plan (architect) and test (tester) stages can run at different
    reasoning efforts despite sharing the claude backend.
    """
    mod_name, cls_name, model_attr, key_attr = BACKENDS[backend]
    provider_class = getattr(importlib.import_module(mod_name), cls_name)
    api_key = None
    if key_attr:
        api_key = getattr(config, key_attr, None) or os.getenv(key_attr.upper(), "")
    # Model precedence, most specific first (blank = unset, `or`-chained so an
    # unset knob transparently drops through — with no role knob set this is
    # byte-identical to the old per-backend-only resolution):
    #   1. GENIUS_MODEL_ROLE_<ROLE>          (per-role env)
    #   2. config.models.roles.<role>        (per-role config)
    #   3. GENIUS_MODEL_<BACKEND>            (per-backend env)
    #   4. config.models.<attr>              (per-backend config)
    # Per-role lets two roles share a backend yet use different models (e.g.
    # researcher and codex both on agy but gemini-pro vs gemini-flash).
    role_model = ""
    if role:
        crole = canonical_role(role)
        role_model = os.environ.get(
            f"GENIUS_MODEL_ROLE_{crole.upper()}", ""
        ) or getattr(config.models.roles, crole, "")
    model_name = (
        role_model
        or os.environ.get(f"GENIUS_MODEL_{backend.upper()}")
        or getattr(config.models, model_attr)
    )
    kwargs = {"api_key": api_key, "model_name": model_name}
    if role:
        kwargs["role"] = role
    return provider_class(**kwargs)


class FallbackProvider:
    """Ordered provider chain: try each backend, fall through on failure.

    Duck-types the ``BaseProvider`` surface the agents use (``send_prompt`` and
    ``model_name``). Inner providers are constructed lazily on first use and
    cached, so an unused fallback backend never resolves its CLI. On
    ``RuntimeError`` (which includes ``CLITimeoutError``) the next backend is
    tried; ``asyncio.CancelledError``/``KeyboardInterrupt`` are never swallowed.

    Sticky success: the backend that last succeeded is tried first on
    subsequent calls, so a dead primary (e.g. grok 403 out-of-credits) is not
    re-paid on every prompt in the same process.
    """

    def __init__(
        self, role: str, backends: List[Tuple[str, Callable[[], Any]]]
    ) -> None:
        if not backends:
            raise ValueError("FallbackProvider needs at least one backend")
        self.role = role
        self._backends = list(backends)
        self._instances: Dict[str, Any] = {}
        self._active_index = 0  # index of the last-successful backend

    @property
    def backend_names(self) -> List[str]:
        return [name for name, _ in self._backends]

    def _provider(self, index: int):
        name, factory = self._backends[index]
        if name not in self._instances:
            self._instances[name] = factory()
        return self._instances[name]

    @property
    def model_name(self) -> str:
        """Model of the active (last-successful) backend, else the first."""
        try:
            return self._provider(self._active_index).model_name
        except Exception:  # noqa: BLE001 - fall back to the primary's model
            return self._provider(0).model_name

    def _call_order(self) -> List[int]:
        order = list(range(len(self._backends)))
        if self._active_index:
            order.remove(self._active_index)
            order.insert(0, self._active_index)
        return order

    async def send_prompt(self, *args: Any, **kwargs: Any) -> Dict[str, Any]:
        order = self._call_order()
        attempted: List[str] = []
        last_exc: Optional[BaseException] = None
        for pos, idx in enumerate(order):
            name = self._backends[idx][0]
            attempted.append(name)
            try:
                provider = self._provider(idx)
                result = await provider.send_prompt(*args, **kwargs)
            except (asyncio.CancelledError, KeyboardInterrupt):
                raise
            except (RuntimeError, OSError) as exc:
                # RuntimeError includes CLITimeoutError; OSError (e.g.
                # FileNotFoundError when a backend's CLI path is wrong or the
                # binary isn't installed) previously escaped the chain and
                # failed the stage even when a healthy fallback backend existed.
                last_exc = exc
                remaining = [self._backends[i][0] for i in order[pos + 1 :]]
                if remaining:
                    logger.warning(
                        "[provider-fallback] %s: backend '%s' failed (%.200s); "
                        "trying '%s'",
                        self.role,
                        name,
                        exc,
                        remaining[0],
                    )
                else:
                    logger.warning(
                        "[provider-fallback] %s: backend '%s' failed (%.200s); "
                        "no backends left",
                        self.role,
                        name,
                        exc,
                    )
                continue
            self._active_index = idx
            return result
        raise RuntimeError(
            f"[provider-fallback] {self.role}: all backends failed "
            f"(attempted: {', '.join(attempted)}); last error from "
            f"'{attempted[-1]}': {last_exc}"
        ) from last_exc


def make_provider(role: str, config, default_chain: Optional[List[str]] = None):
    """Build the provider (or fallback chain) for ``role`` from ``config``.

    ``default_chain`` overrides the role's :data:`DEFAULT_CHAINS` entry for
    call sites whose historical wiring differs (the MCP ``deploy`` tool keeps
    its claude-first tradition with ``["claude", "codex", "agy"]``). An
    explicit ``GENIUS_PROVIDER_<ROLE>`` env chain still wins over it.

    Single-backend chains return the raw provider instance; multi-backend
    chains return a lazy :class:`FallbackProvider`.
    """
    role = canonical_role(role)
    chain = resolve_chain(role)
    if default_chain and not _explicit_chain_env(role):
        unknown = sorted(set(default_chain) - set(BACKENDS))
        if unknown:
            raise ValueError(
                f"default_chain for role {role!r} names unknown backend(s) "
                f"{unknown}; valid backends: {', '.join(sorted(BACKENDS))}"
            )
        chain = [name.lower() for name in default_chain]
    if len(chain) == 1:
        return build_backend(chain[0], config, role=role)
    return FallbackProvider(
        role,
        [
            (name, (lambda n=name: build_backend(n, config, role=role)))
            for name in chain
        ],
    )
