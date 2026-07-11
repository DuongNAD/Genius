"""Tests for the machine-readable security verdict and the blocking gate
(Phase 2 B5 / H2 / H3)."""

import orchestrator
from orchestrator import parse_security_verdict, security_is_blocking


def test_security_is_blocking_respects_patched_detect(monkeypatch):
    # A prose report with no structured verdict falls back to
    # detect_vulnerabilities; patching it at the orchestrator namespace must
    # take effect (security_is_blocking lives in orchestrator so the call
    # resolves there, not in a helper-module copy).
    report = "plain prose security report with no json verdict block"
    assert parse_security_verdict(report) is None
    monkeypatch.setattr(orchestrator, "detect_vulnerabilities", lambda r: True)
    assert orchestrator.security_is_blocking(report) is True
    monkeypatch.setattr(orchestrator, "detect_vulnerabilities", lambda r: False)
    assert orchestrator.security_is_blocking(report) is False


def test_security_is_blocking_respects_patched_verdict(monkeypatch):
    monkeypatch.setattr(
        orchestrator, "parse_security_verdict", lambda r: {"blocking": True}
    )
    assert orchestrator.security_is_blocking("anything") is True


def test_verdict_blocking_true():
    report = (
        "Audit:\n```json\n"
        '{"blocking": true, "findings": [{"severity": "high", "line": 3, '
        '"issue": "SQL injection", "fix": "use parameterized queries"}]}\n```'
    )
    v = parse_security_verdict(report)
    assert v is not None and v["blocking"] is True
    assert security_is_blocking(report) is True


def test_verdict_blocking_false():
    report = '```json\n{"blocking": false, "findings": []}\n```'
    assert security_is_blocking(report) is False


def test_verdict_trusts_blocking_flag_over_keywords():
    # The finding text mentions "high", but blocking is false -> not blocking.
    # The structured flag wins; we must not fall back to keyword matching.
    report = (
        '```json\n{"blocking": false, "findings": [{"severity": "low", '
        '"issue": "high cyclomatic complexity", "fix": "refactor"}]}\n```'
    )
    assert security_is_blocking(report) is False


def test_no_verdict_falls_back_to_prose():
    assert parse_security_verdict("No vulnerabilities found.") is None
    assert security_is_blocking("No vulnerabilities found.") is False
    assert (
        security_is_blocking("Severity: HIGH. eval() on unsanitized user input.")
        is True
    )


def test_empty_report_has_no_verdict():
    assert parse_security_verdict("") is None
    assert security_is_blocking("") is False
