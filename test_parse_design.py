"""Tests for the brace-aware parse_design_for_files (Phase 2 B4)."""
import json

from orchestrator import parse_design_for_files


def test_spec_containing_braces_does_not_truncate():
    # The old `\{.*?\}` regex stopped at the first '}', truncating any spec that
    # contained a brace. The raw_decode scanner must parse the whole object.
    plan = {
        "files": [
            {"path": "src/cfg.py",
             "specification": "Return a config dict like {\"a\": 1, \"b\": {\"c\": 2}} from get_config()."}
        ]
    }
    design = "Here is the plan:\n```json\n" + json.dumps(plan, indent=2) + "\n```\n"
    files = parse_design_for_files(design)
    assert len(files) == 1
    assert files[0]["path"] == "src/cfg.py"
    assert "}" in files[0]["specification"]


def test_unfenced_json_object_is_parsed():
    design = 'Plan: {"files": [{"path": "a.py", "specification": "do a"}]} -- done.'
    files = parse_design_for_files(design)
    assert len(files) == 1
    assert files[0]["path"] == "a.py"


def test_prose_with_stray_braces_falls_back_to_filepath_blocks():
    design = (
        "Some prose with a stray { brace and } here.\n"
        "```python\n# filepath: src/x.py\ndef x():\n    pass\n```\n"
    )
    files = parse_design_for_files(design)
    assert len(files) == 1
    assert files[0]["path"] == "src/x.py"
