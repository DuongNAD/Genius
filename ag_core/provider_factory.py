"""Central provider selection + runtime fallback chains.

The three provider construction sites (``ag_core.skill_app.build_agent``,
``mcp_server.execute_agent`` and ``ag_core.distributed.worker.execute_task``)
all route through :func:`make_provider` so the role -> backend mapping lives in
one place and can be redirected without code changes:

* ``GENIUS_PROVIDER_<ROLE>`` (e.g. ``GENIUS_PROVIDER_GROK=claude,codex``) -
  explicit comma-separated backend chain for one role.
* ``GENIUS_PROVIDER_FALLBACK=1`` (also ``true``/``auto``) - every role uses its
  :data:`DEFAULT_CHAINS` entry, so a backend that dies at runtime (e.g. the
  grok CLI returning 403 out-of-credits) is retried on the next backend.
* Neither set - the legacy single backend per role, bit-identical to the
  pre-fallback wiring (:func:`make_provider` returns the raw provider, not a
  :class:`FallbackProvider`).

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
#                  config.models attr, config api-key attr)
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
}

# role -> preferred backend order when GENIUS_PROVIDER_FALLBACK is enabled.
DEFAULT_CHAINS = {
    "grok": ["grok", "claude", "codex"],
    "claude": ["claude", "codex"],
    "codex": ["codex", "claude"],
    "tester": ["codex", "claude"],
    "security": ["codex", "claude"],
    "devops": ["codex", "claude"],
}

# role -> the single backend each role used before fallback existed. Used when
# no env knob is set so the default path constructs the exact same provider
# class as the legacy per-site wiring.
LEGACY_BACKENDS = {
    "grok": "grok",
    "claude": "claude",
    "codex": "codex",
    "tester": "codex",
    "security": "codex",
    "devops": "codex",
}

_TRUTHY = ("1", "true", "auto", "yes", "on")


def fallback_enabled() -> bool:
    """True when GENIUS_PROVIDER_FALLBACK is set to a truthy value."""
    raw = os.environ.get("GENIUS_PROVIDER_FALLBACK") or ""
    return raw.strip().lower() in _TRUTHY


def _explicit_chain_env(role: str) -> Optional[str]:
    """The raw GENIUS_PROVIDER_<ROLE> value, or None when unset/blank."""
    raw = os.environ.get(f"GENIUS_PROVIDER_{role.upper()}") or ""
    raw = raw.strip()
    return raw or None


def resolve_chain(role: str) -> List[str]:
    """Resolve the ordered backend chain for ``role``.

    Precedence: explicit ``GENIUS_PROVIDER_<ROLE>`` > ``GENIUS_PROVIDER_FALLBACK``
    (auto default chain) > legacy single backend. Raises :class:`ValueError`
    for unknown roles or unknown backend names in the explicit env value.
    """
    role = role.lower()
    if role not in DEFAULT_CHAINS:
        raise ValueError(
            f"Unknown role: {role!r}; expected one of {sorted(DEFAULT_CHAINS)}"
        )

    explicit = _explicit_chain_env(role)
    if explicit:
        chain = [name.strip().lower() for name in explicit.split(",") if name.strip()]
        unknown = sorted(set(chain) - set(BACKENDS))
        if not chain or unknown:
            raise ValueError(
                f"GENIUS_PROVIDER_{role.upper()}={explicit!r} names unknown "
                f"backend(s) {unknown or ['<empty>']}; valid backends: "
                f"{', '.join(sorted(BACKENDS))} (comma-separated, in fallback "
                "order)"
            )
        return chain

    if fallback_enabled():
        return list(DEFAULT_CHAINS[role])

    return [LEGACY_BACKENDS[role]]


def chain_source(role: str) -> Optional[str]:
    """Which knob produced the chain for ``role`` (for diagnostics output).

    Returns ``"GENIUS_PROVIDER_<ROLE>"``, ``"GENIUS_PROVIDER_FALLBACK=<val>"``
    or ``None`` for the legacy default.
    """
    role = role.lower()
    if _explicit_chain_env(role):
        return f"GENIUS_PROVIDER_{role.upper()}"
    if fallback_enabled():
        raw = (os.environ.get("GENIUS_PROVIDER_FALLBACK") or "").strip()
        return f"GENIUS_PROVIDER_FALLBACK={raw}"
    return None


def build_backend(backend: str, config):
    """Instantiate the provider for one backend from ``config``."""
    mod_name, cls_name, model_attr, key_attr = BACKENDS[backend]
    provider_class = getattr(importlib.import_module(mod_name), cls_name)
    api_key = getattr(config, key_attr, None) or os.getenv(key_attr.upper(), "")
    model_name = getattr(config.models, model_attr)
    return provider_class(api_key=api_key, model_name=model_name)


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
            except RuntimeError as exc:  # includes CLITimeoutError
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


def make_provider(role: str, config, legacy_backend: Optional[str] = None):
    """Build the provider (or fallback chain) for ``role`` from ``config``.

    ``legacy_backend`` overrides the no-env single backend for call sites whose
    historical wiring differs from :data:`LEGACY_BACKENDS` (the MCP ``deploy``
    tool has always used the claude backend for the devops role).

    Single-backend chains return the raw provider instance - zero behavior
    change for the default path. Multi-backend chains return a lazy
    :class:`FallbackProvider`.
    """
    role = role.lower()
    chain = resolve_chain(role)
    if legacy_backend and not _explicit_chain_env(role) and not fallback_enabled():
        chain = [legacy_backend]
    if len(chain) == 1:
        return build_backend(chain[0], config)
    return FallbackProvider(
        role,
        [(name, (lambda n=name: build_backend(n, config))) for name in chain],
    )
