"""PromptDirectives — the ``@modifier`` layer of the two-layer prompt syntax.

A prompt may lead with a ``/cmd`` routing token (handled by the existing slash
layers — ``BaseAgent._route_slash_command`` and the orchestrator/serve routing
tables — and left UNTOUCHED here) and/or a run of ``@modifier`` tokens, e.g.::

    /code @deep @critic Implement authentication
    @deep /research Compare SQLite and PostgreSQL
    @variants=3 Draft a retry policy

``parse_directives`` strips the leading ``@modifier`` run (and two legacy
``/HUMAN`` / ``/FLOOD`` aliases), returning a cleaned prompt whose leading token
is still the ``/cmd`` plus a :class:`PromptDirectives` value describing the
requested behaviour. It is deliberately **byte-preserving**: the body is sliced
verbatim from the original string, and when there is nothing to strip it returns
the ORIGINAL string object unchanged so the slash-routing layers (and their
pinned tests) see identical input. The parser only scans a CONTIGUOUS LEADING
run — a modifier-looking token in the middle of the prompt (``explain @table as
a data structure``) is left as literal text.

Effort (@deep) is carried on the value object and threaded to the provider on
the call stack (never via env / instance state), so concurrent jobs don't
interfere. Format/generation modifiers are rendered into a guidance block by the
agent; each agent decides which modifiers it accepts (see BaseAgent
``ACCEPTED_MODIFIERS``), so structure-sensitive stages (/code, /unit_test,
/design, /security_audit) can refuse formatting that would break their parsers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple

# Bounded-generation caps — the explicit guard against a @variants / @ideas
# (migrated /FLOOD) token blow-up.
MAX_VARIANTS = 5
MAX_IDEAS = 20

# @deep maps to this reasoning tier, passed verbatim to the provider (Claude
# --effort / Codex -c model_reasoning_effort). "high" is valid for both.
DEEP_EFFORT = "high"

# Format/style modifiers that render as guidance text (no effort, no structure).
FORMAT_MODIFIERS = frozenset(
    {"simple", "table", "steps", "tight", "natural", "redpen"}
)

# canonical name -> spec. kind:
#   "effort" -> sets .effort (@deep)
#   "flag"   -> boolean field named by `field`
#   "int"    -> optional int field `field`, bare token uses `default`, clamped [1, cap]
#   "format" -> added to .formats
MODIFIER_TABLE = {
    "deep": {"kind": "effort"},
    "critic": {"kind": "flag", "field": "critic"},
    "variants": {"kind": "int", "field": "variants", "default": 3, "cap": MAX_VARIANTS},
    "ideas": {"kind": "int", "field": "ideas", "default": 10, "cap": MAX_IDEAS},
    "simple": {"kind": "format"},
    "table": {"kind": "format"},
    "steps": {"kind": "format"},
    "tight": {"kind": "format"},
    "natural": {"kind": "format"},
    "redpen": {"kind": "format"},
}

# Legacy slash commands re-homed as modifiers (they are not in any SLASH_PREFIXES
# / ROUTING_TABLE, so this is a clean re-home, not a routing change). Matched
# case-insensitively as a leading token and consumed (not preserved as /cmd).
#   /HUMAN -> @natural  (drops the old "evade AI detector" objective entirely)
#   /FLOOD -> @ideas    (was unbounded; now capped at MAX_IDEAS)
_LEGACY_ALIASES = {"/human": "natural", "/flood": "ideas"}

# Convenience for prose agents whose output is consumed verbatim (researcher,
# devops): they accept every modifier. Structure-sensitive agents declare a
# narrow subset (typically just {"deep"}) instead.
ALL_MODIFIERS = frozenset(MODIFIER_TABLE)


@dataclass(frozen=True)
class PromptDirectives:
    """Parsed ``@modifier`` state. A default instance is the canonical no-op."""

    effort: Optional[str] = None
    critic: bool = False
    variants: Optional[int] = None
    ideas: Optional[int] = None
    formats: frozenset = frozenset()
    raw: Tuple[str, ...] = ()      # tokens exactly as seen (telemetry/logging)
    rejected: Tuple[str, ...] = ()  # modifier names dropped by an agent allowlist

    def is_empty(self) -> bool:
        return (
            self.effort is None
            and not self.critic
            and self.variants is None
            and self.ideas is None
            and not self.formats
        )


def _clamp(value: int, cap: int) -> int:
    return max(1, min(value, cap))


def _apply_token(name: str, value: Optional[str], state: dict) -> None:
    """Fold one recognized modifier ``name`` (with optional ``=value``) into the
    mutable ``state`` dict used to build the PromptDirectives."""
    spec = MODIFIER_TABLE[name]
    kind = spec["kind"]
    if kind == "effort":
        state["effort"] = DEEP_EFFORT
    elif kind == "flag":
        state[spec["field"]] = True
    elif kind == "int":
        n = spec["default"]
        if value is not None:
            try:
                n = int(value)
            except ValueError:
                n = spec["default"]  # malformed value -> default (still bounded)
        state[spec["field"]] = _clamp(n, spec["cap"])
    elif kind == "format":
        state["formats"].add(name)


def parse_directives(text: str) -> Tuple[str, PromptDirectives]:
    """Split a prompt into (cleaned_prompt, PromptDirectives).

    Scans a contiguous leading run of tokens that are each either a recognized
    ``@modifier`` (name in :data:`MODIFIER_TABLE`, optionally ``=value``), a
    legacy ``/HUMAN`` / ``/FLOOD`` alias, or the single ``/cmd`` routing token
    (preserved in place). Scanning stops at the first token that is none of
    those; everything from there on is the body, sliced VERBATIM. When no
    modifier or alias is found, returns the original string object unchanged.
    """
    if not text:
        return text, PromptDirectives()

    state = {
        "effort": None,
        "critic": False,
        "variants": None,
        "ideas": None,
        "formats": set(),
    }
    raw: list = []
    cmd: Optional[str] = None
    found = False           # a modifier/alias was consumed (triggers rewrite)
    body_start = 0
    pos = 0
    n = len(text)

    while pos < n:
        # skip whitespace between tokens
        while pos < n and text[pos].isspace():
            pos += 1
        if pos >= n:
            body_start = n
            break
        tok_start = pos
        while pos < n and not text[pos].isspace():
            pos += 1
        token = text[tok_start:pos]
        low = token.lower()

        if token.startswith("@"):
            name, _, value = token[1:].partition("=")
            has_value = "=" in token
            # Case-insensitive like the legacy /HUMAN aliases below, so mobile
            # autocapitalization (`@Deep`, `@Table`) still resolves rather than
            # leaking the literal token into the prompt.
            name = name.lower()
            if name in MODIFIER_TABLE:
                _apply_token(name, value if has_value else None, state)
                raw.append(token)
                found = True
                continue
            # unrecognized @token -> body starts here
            body_start = tok_start
            break
        if low in _LEGACY_ALIASES:
            _apply_token(_LEGACY_ALIASES[low], None, state)
            raw.append(token)
            found = True
            continue
        if token.startswith("/") and cmd is None:
            cmd = token  # the routing token; preserved, does not stop the scan
            continue
        # a plain word (or a second slash token) -> body starts here
        body_start = tok_start
        break
    else:
        body_start = n

    if not found:
        # Nothing to strip -> byte-identical passthrough (original object).
        return text, PromptDirectives()

    body = text[body_start:]
    cleaned = (cmd + " " + body) if cmd else body

    directives = PromptDirectives(
        effort=state["effort"],
        critic=state["critic"],
        variants=state["variants"],
        ideas=state["ideas"],
        formats=frozenset(state["formats"]),
        raw=tuple(raw),
    )
    return cleaned, directives
