"""Pipeline exception types.

Extracted from ``orchestrator.py`` so helper modules can raise them without
importing the large ``orchestrator`` module (which would create a circular
import). ``orchestrator`` re-exports both names, so ``orchestrator.PipelineError``
/ ``orchestrator.ChecksumMismatchError`` and ``from orchestrator import
PipelineError`` keep working unchanged — and the class identity is preserved, so
``except PipelineError`` still catches instances raised from anywhere.
"""


class PipelineError(Exception):
    """Custom exception raised when a pipeline stage fails or validation fails."""


class ChecksumMismatchError(Exception):
    """Custom exception raised when payload checksum validation fails."""
