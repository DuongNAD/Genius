"""Reusable Pytest Fixtures & Dry-Run Test Harness for Genius.

Provides fixtures for 100% deterministic offline testing, provider interception,
isolated workspace management, and chaos fault injection.
"""

import os
import shutil
import tempfile
from typing import Any, Generator, Optional
import pytest
from unittest.mock import patch

from ag_core.testing.offline_mock import (
    OfflineMockProvider,
    patch_cli_subprocess,
)


class OfflineMockContext:
    """Convenience context wrapping an OfflineMockProvider during test execution."""

    def __init__(self, provider: OfflineMockProvider, workspace_dir: Optional[str] = None) -> None:
        self.provider = provider
        self.workspace_dir = workspace_dir

    @property
    def history(self):
        return self.provider.history

    @property
    def call_records(self):
        return self.provider.call_records

    @property
    def call_count(self) -> int:
        return self.provider.call_count

    def inject_fault(self, fault_type: str, attempts: int = 1, pattern: str = ".*", retry_after: float = 0.1, role: Optional[str] = None):
        self.provider.inject_fault(
            fault_type=fault_type, attempts=attempts, pattern=pattern, retry_after=retry_after, role=role
        )

    def clear_faults(self):
        self.provider.clear_faults()

    def add_rule(self, rule):
        self.provider.add_rule(rule)

    def reset(self):
        self.provider.reset()


@pytest.fixture
def offline_mock_provider() -> OfflineMockProvider:
    """Fixture providing a clean OfflineMockProvider instance."""
    return OfflineMockProvider()


class mock_all_providers:
    """Context manager replacing provider factory creation with OfflineMockProvider."""

    def __init__(self, provider: Optional[OfflineMockProvider] = None) -> None:
        self.provider = provider or OfflineMockProvider()
        self._patcher: Optional[Any] = None

    def __enter__(self) -> OfflineMockProvider:
        def _mock_make_provider(role: str, config=None, default_chain=None):
            return self.provider

        def _mock_build_backend(backend: str, config=None, role=None):
            return self.provider

        self._p_make = patch("ag_core.provider_factory.make_provider", side_effect=_mock_make_provider)
        self._p_build = patch("ag_core.provider_factory.build_backend", side_effect=_mock_build_backend)
        self._p_make.start()
        self._p_build.start()
        return self.provider

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        if hasattr(self, "_p_make"):
            self._p_make.stop()
        if hasattr(self, "_p_build"):
            self._p_build.stop()


@pytest.fixture
def offline_genius_env(monkeypatch) -> Generator[OfflineMockContext, None, None]:
    """
    Comprehensive pytest fixture ensuring 100% deterministic offline execution:
    1. Installs mock providers into ag_core.provider_factory.
    2. Intercepts CLI subprocesses with CLIStreamEmulator.
    3. Provides isolated environment variables and HMAC keys.
    """
    monkeypatch.setenv("SKILL_API_KEY", "offline-mock-secret")
    monkeypatch.setenv("GENIUS_CODEX_SANDBOX", "read-only")
    monkeypatch.setenv("GENIUS_CLI_TIMEOUT", "10.0")
    monkeypatch.setenv("GENIUS_TEST_TIMEOUT", "5.0")
    monkeypatch.setenv("GENIUS_DEGRADED_MODE", "0")

    provider = OfflineMockProvider()
    with mock_all_providers(provider), patch_cli_subprocess(provider):
        ctx = OfflineMockContext(provider)
        yield ctx


@pytest.fixture
def dryrun_pipeline(monkeypatch) -> Generator[OfflineMockContext, None, None]:
    """
    Sets up an isolated temporary workspace and pre-configured offline mock provider
    capable of executing the full multi-agent pipeline from end to end.
    """
    temp_dir = tempfile.mkdtemp(prefix="genius_dryrun_")
    monkeypatch.setenv("SKILL_API_KEY", "offline-mock-secret")
    monkeypatch.setenv("GENIUS_CODEX_SANDBOX", "read-only")
    monkeypatch.setenv("GENIUS_CLI_TIMEOUT", "10.0")
    monkeypatch.setenv("GENIUS_TEST_TIMEOUT", "5.0")

    provider = OfflineMockProvider()
    with mock_all_providers(provider), patch_cli_subprocess(provider):
        ctx = OfflineMockContext(provider, workspace_dir=temp_dir)
        try:
            yield ctx
        finally:
            if os.path.exists(temp_dir):
                shutil.rmtree(temp_dir, ignore_errors=True)


@pytest.fixture
def fault_injector(offline_genius_env: OfflineMockContext) -> OfflineMockContext:
    """Fixture providing ready-to-use fault injection capabilities on top of offline_genius_env."""
    return offline_genius_env
