"""Adversarial stress-testing harness for Milestone 1 implementations.

Thoroughly tests:
1. Polyglot code fence extraction (mixed fences, nested backticks, diverse languages, whitespace variations)
2. DevOps artifact parsing & materialization (path traversal, malformed paths, absolute paths, multi-file extraction)
3. Structured debate verdict parsing (boundary scores, negative phrasing, corrupted JSON, multi-block handling)
4. Pydantic model validation boundaries (type coercion, invalid types, extreme values, helper property invariants)
"""

import json
import os
import pytest
from pydantic import ValidationError

from ag_core.models import (
    DesignFile,
    DesignPlan,
    SecurityFinding,
    SecurityReport,
    DevOpsArtifact,
    DevOpsPlan,
    DebateVerdict,
)
from ag_core.utils.code_extract import extract_code
from orchestrator import (
    parse_devops_artifacts,
    materialize_devops_artifacts,
    parse_debate_verdict,
    is_debate_approved,
    safe_join,
    PipelineError,
)


# ==============================================================================
# Tier 1: Polyglot Code Extraction Stress Tests
# ==============================================================================


class TestPolyglotCodeExtraction:
    """Stress-test code fence extraction across multiple languages, fence formats, and noisy outputs."""

    def test_extract_yaml_fence(self):
        content = (
            "Here is the configuration file:\n"
            "```yaml\n"
            "server:\n"
            "  port: 8080\n"
            "  host: 0.0.0.0\n"
            "```\n"
        )
        extracted = extract_code(content)
        assert extracted == "server:\n  port: 8080\n  host: 0.0.0.0"

    def test_extract_dockerfile_fence(self):
        content = (
            "```dockerfile\n"
            "FROM alpine:3.18\n"
            "RUN apk add --no-cache curl\n"
            "ENTRYPOINT [\"curl\"]\n"
            "```"
        )
        extracted = extract_code(content)
        assert extracted == "FROM alpine:3.18\nRUN apk add --no-cache curl\nENTRYPOINT [\"curl\"]"

    def test_extract_json_fence(self):
        content = (
            "```json\n"
            "{\n"
            "  \"name\": \"genius-app\",\n"
            "  \"version\": \"2.0.0\"\n"
            "}\n"
            "```"
        )
        extracted = extract_code(content)
        assert json.loads(extracted) == {"name": "genius-app", "version": "2.0.0"}

    def test_extract_sql_fence(self):
        content = (
            "```sql\n"
            "CREATE TABLE users (\n"
            "    id SERIAL PRIMARY KEY,\n"
            "    username VARCHAR(50) NOT NULL\n"
            ");\n"
            "```"
        )
        extracted = extract_code(content)
        assert "CREATE TABLE users" in extracted

    def test_extract_mixed_polyglot_non_python_largest_wins(self):
        content = (
            "Quick config:\n"
            "```yaml\n"
            "short: true\n"
            "```\n"
            "Full docker-compose:\n"
            "```yaml\n"
            "version: '3.8'\n"
            "services:\n"
            "  app:\n"
            "    image: my-app:latest\n"
            "    ports:\n"
            "      - '8000:8000'\n"
            "```"
        )
        extracted = extract_code(content)
        assert "services:" in extracted
        assert "short: true" not in extracted

    def test_extract_python_preferred_over_larger_other_blocks(self):
        # Even if a YAML block or bash block is larger, a Python block takes priority when targeting python
        bash_block = "\n".join(f"echo 'Step {i}'" for i in range(20))
        content = (
            f"```bash\n{bash_block}\n```\n\n"
            "```python\ndef main():\n    print('Hello World')\n```"
        )
        extracted = extract_code(content)
        assert extracted == "def main():\n    print('Hello World')"

    def test_extract_nested_markdown_in_docstring(self):
        code_with_docstring = (
            "```python\n"
            "def generate_docs():\n"
            "    \"\"\"\n"
            "    Example usage:\n"
            "    `inline_code()`\n"
            "    \"\"\"\n"
            "    return True\n"
            "```"
        )
        extracted = extract_code(code_with_docstring)
        assert "def generate_docs():" in extracted
        assert "`inline_code()`" in extracted

    def test_extract_crlf_windows_newlines(self):
        crlf_content = "```python\r\ndef windows_func():\r\n    return 'ok'\r\n```"
        extracted = extract_code(crlf_content)
        assert "def windows_func():" in extracted

    def test_extract_empty_or_whitespace_fences(self):
        assert extract_code("```python\n```") == ""
        assert extract_code("   \n\t\n  ") == ""
        assert extract_code("") == ""


# ==============================================================================
# Tier 2: DevOps Materialization & Path Traversal Defense Stress Tests
# ==============================================================================


class TestDevOpsMaterialization:
    """Stress-test parsing and filesystem materialization against malicious/malformed inputs."""

    def test_parse_multi_language_filepath_comment_styles(self):
        multi_comment_devops = (
            "```dockerfile\n"
            "# filepath: Dockerfile\n"
            "FROM python:3.11\n"
            "```\n\n"
            "```javascript\n"
            "// filepath: frontend/app.js\n"
            "console.log('web');\n"
            "```\n\n"
            "```sql\n"
            "-- filepath: db/migrations/001.sql\n"
            "CREATE TABLE logs (id INT);\n"
            "```\n\n"
            "```html\n"
            "<!-- filepath: public/index.html -->\n"
            "<h1>Hello</h1>\n"
            "```\n"
        )
        artifacts = parse_devops_artifacts(multi_comment_devops)
        assert len(artifacts) == 4
        paths = [a.path for a in artifacts]
        assert paths == [
            "Dockerfile",
            "frontend/app.js",
            "db/migrations/001.sql",
            "public/index.html",
        ]

    def test_parse_quoted_filepaths(self):
        devops_text = (
            "```yaml\n"
            "# filepath: \"config/staging.yaml\"\n"
            "env: staging\n"
            "```\n\n"
            "```sh\n"
            "# filepath: 'scripts/deploy.sh'\n"
            "#!/bin/bash\n"
            "```\n"
        )
        artifacts = parse_devops_artifacts(devops_text)
        assert len(artifacts) == 2
        assert artifacts[0].path == "config/staging.yaml"
        assert artifacts[1].path == "scripts/deploy.sh"

    def test_materialize_path_traversal_attempts_blocked(self, tmp_path):
        project_dir = str(tmp_path / "sandbox")
        os.makedirs(project_dir, exist_ok=True)

        traversal_payloads = [
            "../escape1.txt",
            "../../escape2.txt",
            "nested/../../../escape3.txt",
            "a/b/../../../../escape4.txt",
            "..\\..\\escape5.txt",
            "a\\..\\..\\escape6.txt",
        ]

        for payload in traversal_payloads:
            content = f"```text\n# filepath: {payload}\nmalicious payload\n```"
            materialized = materialize_devops_artifacts(content, project_dir)
            assert len(materialized) == 0, f"Failed to reject traversal path: {payload}"

        # Confirm no files were created outside project_dir
        assert not os.path.exists(tmp_path / "escape1.txt")
        assert not os.path.exists(tmp_path / "escape2.txt")
        assert not os.path.exists(tmp_path / "escape3.txt")

    def test_materialize_absolute_paths_blocked(self, tmp_path):
        project_dir = str(tmp_path / "sandbox")
        os.makedirs(project_dir, exist_ok=True)

        absolute_payloads = [
            "/etc/passwd",
            "/var/log/syslog",
            "C:\\Windows\\System32\\calc.exe",
            "C:/secret.txt",
            "D:\\data\\dump.sql",
        ]

        for payload in absolute_payloads:
            content = f"```text\n# filepath: {payload}\nmalicious payload\n```"
            materialized = materialize_devops_artifacts(content, project_dir)
            assert len(materialized) == 0, f"Failed to reject absolute path: {payload}"

    def test_safe_join_unit_invariants(self, tmp_path):
        base = str(tmp_path / "base")
        os.makedirs(base, exist_ok=True)

        # Valid joins
        assert safe_join(base, "file.txt") == os.path.realpath(os.path.join(base, "file.txt"))
        assert safe_join(base, "subdir/nested.txt") == os.path.realpath(os.path.join(base, "subdir", "nested.txt"))
        assert safe_join(base, "./local.txt") == os.path.realpath(os.path.join(base, "local.txt"))

        # Invalid joins must raise PipelineError
        with pytest.raises(PipelineError):
            safe_join(base, "")
        with pytest.raises(PipelineError):
            safe_join(base, "../outside.txt")
        with pytest.raises(PipelineError):
            safe_join(base, "a/../../outside.txt")
        with pytest.raises(PipelineError):
            safe_join(base, "/etc/shadow")
        with pytest.raises(PipelineError):
            safe_join(base, "C:\\test.txt")
        with pytest.raises(PipelineError):
            safe_join(base, "C:foo.txt")

    def test_materialize_empty_blocks_or_missing_paths(self, tmp_path):
        project_dir = str(tmp_path / "sandbox")
        os.makedirs(project_dir, exist_ok=True)

        # Block without filepath
        content = "```yaml\nversion: '3'\n```"
        assert len(materialize_devops_artifacts(content, project_dir)) == 0

        # Block with empty content after filepath
        empty_content = "```yaml\n# filepath: empty.yaml\n```"
        materialized = materialize_devops_artifacts(empty_content, project_dir)
        # It contains `# filepath: empty.yaml`, which is valid non-empty content
        assert len(materialized) == 1
        assert os.path.exists(os.path.join(project_dir, "empty.yaml"))

    def test_materialize_nested_subdirectories_creation(self, tmp_path):
        project_dir = str(tmp_path / "sandbox")
        os.makedirs(project_dir, exist_ok=True)

        content = (
            "```yaml\n"
            "# filepath: deep/nested/infra/k8s/deploy.yaml\n"
            "apiVersion: apps/v1\n"
            "```"
        )
        materialized = materialize_devops_artifacts(content, project_dir)
        assert len(materialized) == 1
        target_file = os.path.join(project_dir, "deep", "nested", "infra", "k8s", "deploy.yaml")
        assert os.path.exists(target_file)
        with open(target_file, "r", encoding="utf-8") as f:
            assert "apiVersion: apps/v1" in f.read()


# ==============================================================================
# Tier 3: Debate Verdict Parsing & Heuristic Stress Tests
# ==============================================================================


class TestDebateVerdictParsing:
    """Stress-test debate parsing with boundary scores, negative phrasing, and corrupted JSON."""

    def test_verdict_explicit_approval_true(self):
        text = """```json
{
  "approved": true,
  "score": 4.5,
  "criteria_scores": {"completeness": 4.5, "security": 4.5},
  "strengths": ["Well designed"],
  "flaws": []
}
```"""
        verdict = parse_debate_verdict(text)
        assert verdict is not None
        assert verdict.approved is True
        assert verdict.is_approved is True
        assert is_debate_approved(text) is True

    def test_verdict_explicit_rejection_false(self):
        text = """```json
{
  "approved": false,
  "score": 2.0,
  "flaws": ["Missing Redis fallback"]
}
```"""
        verdict = parse_debate_verdict(text)
        assert verdict is not None
        assert verdict.approved is False
        assert verdict.is_approved is False
        assert is_debate_approved(text) is False

    def test_verdict_implicit_approval_high_score_no_flaws(self):
        # approved=False but score >= 4.0 and flaws=[]
        text = """```json
{
  "approved": false,
  "score": 4.2,
  "flaws": []
}
```"""
        verdict = parse_debate_verdict(text)
        assert verdict is not None
        assert verdict.is_approved is True
        assert is_debate_approved(text) is True

    def test_verdict_rejection_high_score_with_flaws(self):
        # score >= 4.0 but flaws is NOT empty
        text = """```json
{
  "approved": false,
  "score": 4.5,
  "flaws": ["Minor missing typing on helper"]
}
```"""
        verdict = parse_debate_verdict(text)
        assert verdict is not None
        assert verdict.is_approved is False
        assert is_debate_approved(text) is False

    def test_verdict_boundary_score_below_4_no_flaws(self):
        # score 3.99 with no flaws -> not approved
        text = """```json
{
  "approved": false,
  "score": 3.99,
  "flaws": []
}
```"""
        verdict = parse_debate_verdict(text)
        assert verdict is not None
        assert verdict.is_approved is False
        assert is_debate_approved(text) is False

    def test_negative_phrasing_with_json_rejection(self):
        # When critic outputs negative phrasing containing '[APPROVED]' word, but structured JSON is rejected
        text = (
            "The plan is not ready and cannot be [APPROVED] at this stage.\n"
            "```json\n"
            "{\n"
            "  \"approved\": false,\n"
            "  \"score\": 2.0,\n"
            "  \"flaws\": [\"Unresolved security risks\"]\n"
            "}\n"
            "```"
        )
        assert is_debate_approved(text) is False

    def test_corrupted_json_falls_back_to_marker(self):
        corrupted_text = (
            "```json\n"
            "{ approved: true, score: 5.0 -- syntax error }\n"
            "```\n"
            "Overall, the plan is verified. [APPROVED]"
        )
        assert parse_debate_verdict(corrupted_text) is None
        assert is_debate_approved(corrupted_text) is True

    def test_corrupted_json_without_marker_fails(self):
        corrupted_text = (
            "```json\n"
            "{ approved: false, score: 2.0 -- syntax error }\n"
            "```\n"
            "Plan needs complete overhaul."
        )
        assert parse_debate_verdict(corrupted_text) is None
        assert is_debate_approved(corrupted_text) is False

    def test_multi_json_blocks_prefers_first_valid(self):
        content = (
            "Example format:\n"
            "```json\n"
            "{\"approved\": false, \"score\": 1.0, \"flaws\": [\"example flaw\"]}\n"
            "```\n"
            "Actual verdict:\n"
            "```json\n"
            "{\"approved\": true, \"score\": 5.0, \"flaws\": []}\n"
            "```\n"
        )
        verdict = parse_debate_verdict(content)
        assert verdict is not None
        assert verdict.score == 1.0


# ==============================================================================
# Tier 4: Pydantic Schema Validation & Boundary Invariants
# ==============================================================================


class TestPydanticBoundaries:
    """Stress-test Pydantic model validation, field constraints, and helper methods."""

    def test_design_file_missing_required_fields(self):
        with pytest.raises(ValidationError):
            DesignFile(path="src/app.py")  # missing specification

        with pytest.raises(ValidationError):
            DesignFile(specification="Spec text")  # missing path

    def test_design_plan_defaults_and_serialization(self):
        plan = DesignPlan()
        assert plan.project_name == "project"
        assert plan.files == []
        assert plan.dependencies == []
        assert plan.devops == {}

        # Roundtrip JSON serialization
        json_data = plan.model_dump_json() if hasattr(plan, "model_dump_json") else plan.json()
        assert "project" in json_data

    def test_security_finding_severity_case_insensitivity(self):
        severities_blocking = ["critical", "CRITICAL", "Critical", "high", "HIGH", "High"]
        severities_non_blocking = ["medium", "MEDIUM", "low", "LOW", "info", "informational"]

        for s in severities_blocking:
            f = SecurityFinding(severity=s, issue="test")
            assert f.is_blocking is True, f"Failed for severity {s}"

        for s in severities_non_blocking:
            f = SecurityFinding(severity=s, issue="test")
            assert f.is_blocking is False, f"Failed for severity {s}"

    def test_security_report_aggregations(self):
        f_crit = SecurityFinding(severity="critical", issue="RCE")
        f_high = SecurityFinding(severity="high", issue="SQLi")
        f_med = SecurityFinding(severity="medium", issue="Missing header")
        f_low = SecurityFinding(severity="low", issue="Info leak")

        rep = SecurityReport(findings=[f_crit, f_high, f_med, f_low])
        assert len(rep.critical_findings) == 1
        assert len(rep.high_findings) == 1
        assert len(rep.blocking_findings) == 2
        assert f_crit in rep.critical_findings
        assert f_high in rep.high_findings
        assert f_med not in rep.blocking_findings

    def test_devops_artifact_validation(self):
        with pytest.raises(ValidationError):
            DevOpsArtifact(path="Dockerfile")  # missing content

        with pytest.raises(ValidationError):
            DevOpsArtifact(content="FROM alpine")  # missing path

        art = DevOpsArtifact(path="Dockerfile", content="FROM alpine")
        assert art.path == "Dockerfile"
        assert art.content == "FROM alpine"
        assert art.description == ""

    def test_debate_verdict_property_matrix(self):
        # approved=True -> always True regardless of score/flaws
        assert DebateVerdict(approved=True, score=1.0, flaws=["fatal"]).is_approved is True
        assert DebateVerdict(approved=True, score=5.0, flaws=[]).is_approved is True

        # approved=False -> depends on score >= 4.0 and not flaws
        assert DebateVerdict(approved=False, score=4.0, flaws=[]).is_approved is True
        assert DebateVerdict(approved=False, score=5.0, flaws=[]).is_approved is True
        assert DebateVerdict(approved=False, score=3.999, flaws=[]).is_approved is False
        assert DebateVerdict(approved=False, score=4.5, flaws=["some flaw"]).is_approved is False
        assert DebateVerdict(approved=False, score=2.0, flaws=["error"]).is_approved is False

    def test_devops_plan_validation(self):
        plan = DevOpsPlan(
            summary="Deployment Plan",
            artifacts=[DevOpsArtifact(path="Dockerfile", content="FROM python:3.11")],
        )
        assert plan.summary == "Deployment Plan"
        assert len(plan.artifacts) == 1
        assert plan.artifacts[0].path == "Dockerfile"
