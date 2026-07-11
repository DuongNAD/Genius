import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from ag_core.directives import (  # noqa: E402
    parse_directives,
    PromptDirectives,
    DEEP_EFFORT,
    MAX_VARIANTS,
    MAX_IDEAS,
)


# --- no-op / byte-identity ---------------------------------------------------


def test_no_directive_returns_original_object_and_empty():
    text = "Compare SQLite and PostgreSQL"
    cleaned, d = parse_directives(text)
    assert cleaned is text  # same object -> byte-identical passthrough
    assert d.is_empty()


def test_plain_slash_command_untouched():
    # A /cmd with no @modifier must pass through unchanged (slash layer owns it).
    text = "/design build the retry flow"
    cleaned, d = parse_directives(text)
    assert cleaned is text
    assert d.is_empty()


def test_empty_string():
    cleaned, d = parse_directives("")
    assert cleaned == ""
    assert d.is_empty()


# --- effort (@deep) ----------------------------------------------------------


def test_deep_sets_effort_not_text():
    cleaned, d = parse_directives("@deep Explain consensus")
    assert d.effort == DEEP_EFFORT
    assert cleaned == "Explain consensus"  # @deep stripped from text


def test_deep_with_leading_cmd_preserved():
    cleaned, d = parse_directives("/research @deep Compare A and B")
    assert cleaned == "/research Compare A and B"
    assert d.effort == DEEP_EFFORT


def test_modifier_before_cmd_still_preserves_cmd_first():
    # Breakage-risk #2: '@deep /code ...' must yield a cleaned prompt whose
    # leading token is still /code so Layer-2 routing matches.
    cleaned, d = parse_directives("@deep /code implement login")
    assert cleaned == "/code implement login"
    assert cleaned.split()[0] == "/code"
    assert d.effort == DEEP_EFFORT


# --- flags / critic ----------------------------------------------------------


def test_critic_flag():
    cleaned, d = parse_directives("@critic Assess this plan")
    assert d.critic is True
    assert cleaned == "Assess this plan"


# --- generation modifiers + clamping ----------------------------------------


def test_variants_bare_defaults_to_three():
    _, d = parse_directives("@variants Draft options")
    assert d.variants == 3


def test_variants_value_and_clamp():
    _, d = parse_directives("@variants=3 x")
    assert d.variants == 3
    _, d2 = parse_directives("@variants=99 x")
    assert d2.variants == MAX_VARIANTS
    _, d3 = parse_directives("@variants=0 x")
    assert d3.variants == 1  # clamped up to 1


def test_variants_malformed_value_falls_back_to_default():
    _, d = parse_directives("@variants=abc x")
    assert d.variants == 3


def test_ideas_clamped_to_cap():
    _, d = parse_directives("@ideas=999 brainstorm")
    assert d.ideas == MAX_IDEAS


# --- formats -----------------------------------------------------------------


def test_multiple_formats_accumulate():
    cleaned, d = parse_directives("@table @steps @tight Summarize")
    assert d.formats == frozenset({"table", "steps", "tight"})
    assert cleaned == "Summarize"


# --- legacy aliases ----------------------------------------------------------


def test_human_alias_maps_to_natural_no_detector_evasion():
    cleaned, d = parse_directives("/HUMAN write an intro")
    assert "natural" in d.formats
    assert cleaned == "write an intro"
    # the modifier carries no "evade AI detector" semantics — it is just a format


def test_flood_alias_maps_to_capped_ideas():
    _, d = parse_directives("/FLOOD give me options")
    assert d.ideas is not None and d.ideas <= MAX_IDEAS


# --- leading-run-only contract + body preservation ---------------------------


def test_mid_prompt_modifier_is_not_stripped():
    text = "explain @table as a data structure"
    cleaned, d = parse_directives(text)
    assert cleaned is text  # nothing at the very front -> untouched
    assert d.is_empty()


def test_unknown_modifier_starts_body():
    text = "@unknownmod do the thing"
    cleaned, d = parse_directives(text)
    assert cleaned is text
    assert d.is_empty()


def test_body_bytes_preserved_including_inlined_code():
    code = "def f( x ):\n    return  x*2   # keep   spacing"
    text = f"/code @deep {code}"
    cleaned, d = parse_directives(text)
    assert cleaned == f"/code {code}"  # exact inner bytes preserved
    assert d.effort == DEEP_EFFORT


def test_directive_only_prompt_yields_empty_body():
    # base_agent applies DEFAULT_TASK afterwards; parser just returns "".
    cleaned, d = parse_directives("@deep")
    assert cleaned == ""
    assert d.effort == DEEP_EFFORT


def test_raw_tokens_recorded():
    _, d = parse_directives("@deep @variants=3 x")
    assert d.raw == ("@deep", "@variants=3")


def test_default_directives_is_empty():
    assert PromptDirectives().is_empty()


# --- case-insensitivity (mobile autocapitalization) --------------------------


def test_modifier_case_insensitive_effort():
    cleaned, d = parse_directives("@Deep Explain consensus")
    assert d.effort == DEEP_EFFORT
    assert cleaned == "Explain consensus"  # @Deep stripped, not leaked


def test_modifier_case_insensitive_format_and_value():
    _, d = parse_directives("@TABLE @Variants=3 Summarize")
    assert "table" in d.formats
    assert d.variants == 3
