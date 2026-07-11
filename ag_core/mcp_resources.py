"""MCP resource serving for the Genius server (genius:// artifact URIs).

Extracted from ``mcp_server.py``. A strict whitelist of root-level pipeline
artifacts is the only thing ever listed or read — never a glob — so user files
cannot leak. ``mcp_server`` re-imports these names so every existing reference
(and tests patching ``mcp_server._read_resource``) keeps working.
"""

import os
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
    "review.md": "Code review + lint/test logs produced by the code stage (Codex).",
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
# workspaces, so without this fallback every artifacts_ready URI advertised
# by orchestrate_status would 404 (-32002) on a follow-up resources/read.
_ARTIFACT_WORKSPACES: Dict[str, str] = {}


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

    The artifact name must match the whitelist exactly - same traversal
    posture as orchestrator.safe_join: no separators, no '..', no absolute
    paths can ever reach the filesystem join below.
    """
    catalog = _resource_catalog()
    name = None
    if isinstance(uri, str) and uri.startswith(RESOURCE_URI_PREFIX):
        name = uri[len(RESOURCE_URI_PREFIX) :]
    if not name or name not in catalog:
        raise ResourceNotFoundError(
            f"Unknown resource URI: {uri!r}. Valid URIs are "
            f"{RESOURCE_URI_PREFIX}<name> where <name> is one of the pipeline "
            "artifacts reported by resources/list."
        )
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
