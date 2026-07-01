"""Unit tests for orchestrator.detect_vulnerabilities.

Verifies the security-report vulnerability detector avoids the false positives
that the previous case-sensitive substring matching produced, while still
catching genuine high/critical findings.
"""

from orchestrator import detect_vulnerabilities


# --- Should NOT flag (false positives the old logic produced) ---


def test_no_high_severity_phrase_is_clean():
    assert (
        detect_vulnerabilities("Audit complete: no HIGH severity issues found.")
        is False
    )


def test_zero_critical_is_clean():
    assert (
        detect_vulnerabilities("0 critical vulnerabilities detected. Code looks safe.")
        is False
    )


def test_highly_recommended_is_clean():
    assert (
        detect_vulnerabilities("It is HIGHLY recommended to add docstrings.") is False
    )


def test_highlight_word_is_clean():
    assert detect_vulnerabilities("We highlight a few style nitpicks below.") is False


def test_empty_report_is_clean():
    assert detect_vulnerabilities("") is False
    assert detect_vulnerabilities(None) is False


def test_generic_clean_report():
    assert (
        detect_vulnerabilities("No vulnerabilities found. The code is secure.") is False
    )


# --- Should flag (genuine findings) ---


def test_explicit_marker():
    assert (
        detect_vulnerabilities(
            "Finding: [VULNERABILITY DETECTED] SQL injection in query()."
        )
        is True
    )


def test_insecure_marker():
    assert (
        detect_vulnerabilities("Result: [INSECURE] hardcoded credentials present.")
        is True
    )


def test_high_severity_finding():
    assert (
        detect_vulnerabilities(
            "Severity: HIGH. Unsanitized user input flows into eval()."
        )
        is True
    )


def test_critical_finding_lowercase():
    assert (
        detect_vulnerabilities(
            "This is a critical vulnerability: remote code execution."
        )
        is True
    )


def test_high_risk_finding():
    assert (
        detect_vulnerabilities("This introduces a high risk of path traversal.") is True
    )


def test_mixed_report_with_real_finding():
    report = (
        "Summary: 1 issue.\n"
        "- Style: highly verbose logging (low priority).\n"
        "- Security: CRITICAL - secrets committed to source.\n"
    )
    assert detect_vulnerabilities(report) is True
