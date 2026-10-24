"""Offline Mock & Dry-Run Test Infrastructure for Genius.

Provides 100% deterministic, offline mock providers, CLI stream emulators,
subprocess interception, fault injectors, and pytest fixtures.
"""

from ag_core.testing.offline_mock import (
    CLIStreamEmulator,
    FaultInjectionRule,
    MockCLIProcess,
    MockScenarioRule,
    OfflineMockProvider,
    create_mock_cli_spawn,
    patch_cli_subprocess,
)
from ag_core.testing.fixtures import (
    OfflineMockContext,
    dryrun_pipeline,
    fault_injector,
    mock_all_providers,
    offline_genius_env,
    offline_mock_provider,
)

__all__ = [
    "CLIStreamEmulator",
    "MockScenarioRule",
    "FaultInjectionRule",
    "OfflineMockProvider",
    "MockCLIProcess",
    "create_mock_cli_spawn",
    "patch_cli_subprocess",
    "offline_mock_provider",
    "mock_all_providers",
    "offline_genius_env",
    "dryrun_pipeline",
    "fault_injector",
    "OfflineMockContext",
]
