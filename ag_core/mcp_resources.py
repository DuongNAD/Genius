"""MCP resource serving for the Genius server (genius:// artifact URIs).

Extracted from ``mcp_server.py``. A strict whitelist of pipeline artifact
names is the only thing ever listed or read — never a glob — so user files
cannot leak. Two URI forms exist: legacy bare names
(``genius://artifacts/<name>``, CWD-first) and job-scoped
(``genius://artifacts/<job_id>/<name>``, resolved ONLY inside that job's
workspace — the form orchestrate_status advertises). ``mcp_server``
re-imports these names so every existing reference (and tests patching
``mcp_server._read_resource``) keeps working.
"""

import os
import re
from typing import Dict, List

# --- MCP resources: pipeline artifacts as genius:// URIs -------------------
# Whitelist of root-level markdown artifacts the pipeline produces, keyed by
# file name. Only these names (plus their .bak archives) are ever listed or
# served - never glob arbitrary workspace files, so user files cannot leak.
RESOURCE_URI_PREFIX = "genius://artifacts/"

_RESOURCE_ARTIFACTS = {
    "research.md": (
        "Requirements research produced by the research stage (researcher role)."
    ),
    "design.md": "Architecture design produced by the design stage (Claude).",
    "review.md": (
        "Verification summary assembled by the pipeline: per-file self-heal "
        "results and verification coverage; plus conformance, whole-project "
        "pytest and the final-review sections on the custom flow."
    ),
    "audit.md": "Security audit produced by the security stage.",
    "deploy.md": "Deployment plan produced by the deploy stage (DevOps).",
    "plan.md": "End-to-end plan produced by the e2e pipeline (Claude).",
}


class ResourceNotFoundError(Exception):
    """Maps to JSON-RPC error -32002 (MCP: resource not found)."""


def _resource_catalog() -> Dict[str, str]:
    """name -> description for every servable artifact (incl. .bak archives)."""
    catalog: Dict[str, str] = {}
    for name, desc in _RESOURCE_ARTIFACTS.items():
        catalog[name] = desc
        catalog[name + ".bak"] = (
            f"Archived previous-run copy of {name} (renamed on pipeline start)."
        )
    return catalog


# artifact file name -> workspace of the most recent job observed (by
# orchestrate_status/_stage_progress) to have produced it. resources/read
# and resources/list serve the CWD first (the long-standing behavior), then
# fall back here: orchestrate jobs default to isolated .genius_jobs/<id>
# workspaces, so without this fallback every LEGACY bare-name URI would 404
# (-32002) on a follow-up resources/read. Kept for bare-name compatibility
# only — orchestrate_status now advertises JOB-SCOPED URIs (below), because
# a bare name is ambiguous: a stale root-workspace file or a concurrent job
# can shadow the artifact the client actually asked about.
_ARTIFACT_WORKSPACES: Dict[str, str] = {}

# Job-scoped artifact URIs: genius://artifacts/<job_id>/<name>, where job_id
# is the 32-hex orchestrate job id. These resolve ONLY inside that job's
# workspace (no CWD fallback, no cross-job fallback), so concurrent jobs and
# stale root artifacts can never bleed into each other's reads.
_JOB_URI_ID_RE = re.compile(r"^[0-9a-f]{32}$")
_JOB_WORKSPACES: Dict[str, str] = {}
# Optional fallback resolver (job_id -> workspace path or None) injected by
# mcp_server so a job-scoped URI survives a server restart: the resolver
# rebuilds the mapping from the job's on-disk job.json manifest.
_JOB_WORKSPACE_RESOLVER = None


def register_job_workspace(job_id: str, workspace: str) -> None:
    """Remember which workspace holds a job's artifacts (idempotent)."""
    if job_id and workspace:
        _JOB_WORKSPACES[str(job_id)] = workspace


def set_job_workspace_resolver(resolver) -> None:
    """Install the job_id -> workspace fallback used on registry misses."""
    global _JOB_WORKSPACE_RESOLVER
    _JOB_WORKSPACE_RESOLVER = resolver


def job_artifact_uri(job_id: str, name: str) -> str:
    """The job-scoped genius:// URI for one of a job's whitelisted artifacts."""
    return f"{RESOURCE_URI_PREFIX}{job_id}/{name}"


def _job_workspace(job_id: str):
    """Workspace for a job id, via the registry then the injected resolver."""
    workspace = _JOB_WORKSPACES.get(job_id)
    if workspace:
        return workspace
    if _JOB_WORKSPACE_RESOLVER is not None:
        try:
            workspace = _JOB_WORKSPACE_RESOLVER(job_id)
        except Exception:
            workspace = None
        if workspace:
            _JOB_WORKSPACES[job_id] = workspace
    return workspace


def _list_resources(workspace: str = None) -> List[Dict[str, str]]:
    """Enumerate the whitelisted artifacts that exist in the workspace
    (or, failing that, in the last job workspace observed to hold them)."""
    root = workspace or os.getcwd()
    resources = []
    for name, desc in _resource_catalog().items():
        present = os.path.isfile(os.path.join(root, name))
        if not present:
            alt = _ARTIFACT_WORKSPACES.get(name)
            present = bool(
                alt and alt != root and os.path.isfile(os.path.join(alt, name))
            )
        if present:
            resources.append(
                {
                    "uri": RESOURCE_URI_PREFIX + name,
                    "name": name,
                    "description": desc,
                    "mimeType": "text/markdown",
                }
            )
    return resources


def _read_resource(uri: str, workspace: str = None) -> List[Dict[str, str]]:
    """Return the MCP `contents` blocks for a genius://artifacts/ URI.

    Two URI forms are served:

    - ``genius://artifacts/<name>`` — legacy bare name: the CWD (or the
      ``workspace`` argument) copy first, then the last job observed to
      produce ``<name>``. Ambiguous by construction; kept for compatibility.
    - ``genius://artifacts/<job_id>/<name>`` — job-scoped, what
      orchestrate_status advertises in ``artifacts_ready``: ONLY that job's
      workspace is read, so a stale root-workspace artifact or a concurrent
      job can never shadow the artifact the client asked about.

    The artifact name must match the whitelist exactly and the job id must be
    32 lowercase hex - same traversal posture as orchestrator.safe_join: no
    other separators, no '..', no absolute paths can ever reach the
    filesystem join below.
    """
    catalog = _resource_catalog()
    rest = None
    if isinstance(uri, str) and uri.startswith(RESOURCE_URI_PREFIX):
        rest = uri[len(RESOURCE_URI_PREFIX) :]
    unknown = ResourceNotFoundError(
        f"Unknown resource URI: {uri!r}. Valid URIs are "
        f"{RESOURCE_URI_PREFIX}<name> or {RESOURCE_URI_PREFIX}<job_id>/<name> "
        "where <name> is one of the pipeline artifacts reported by "
        "resources/list."
    )
    if not rest:
        raise unknown
    if "/" in rest:
        job_id, _, name = rest.partition("/")
        if not _JOB_URI_ID_RE.fullmatch(job_id) or name not in catalog:
            raise unknown
        job_workspace = _job_workspace(job_id)
        if not job_workspace:
            raise ResourceNotFoundError(
                f"Unknown job '{job_id}' for resource {uri!r} - this server "
                "process has not seen the job. Call orchestrate_status with "
                "the job_id first (it re-registers recovered jobs), then "
                "retry."
            )
        candidates = [os.path.join(job_workspace, name)]
    else:
        name = rest
        if name not in catalog:
            raise unknown
        root = workspace or os.getcwd()
        candidates = [os.path.join(root, name)]
        alt = _ARTIFACT_WORKSPACES.get(name)
        if alt and alt != root:
            candidates.append(os.path.join(alt, name))
    for path in candidates:
        try:
            with open(path, "r", encoding="utf-8") as f:
                text = f.read()
        except OSError:
            continue
        except UnicodeDecodeError:
            # A whitelisted artifact that isn't valid UTF-8 is corruption, not a
            # reason to crash: re-read best-effort with replacement chars rather
            # than letting UnicodeDecodeError escape as an unhandled error.
            with open(path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        return [{"uri": uri, "mimeType": "text/markdown", "text": text}]
    raise ResourceNotFoundError(
        f"Artifact '{name}' does not exist yet - the pipeline stage that "
        "produces it has not completed. Poll orchestrate_status."
    )
