# Distributed Agent Network E2E Test Suite Infrastructure

## 1. Test Philosophy
The test suite utilizes a comprehensive, multi-tiered testing strategy designed to verify behavior under both normal operations and edge conditions without relying on implementation details of the specific underlying LLMs.
- **Opaque-box, requirement-driven**: The tests evaluate the system based purely on its inputs, outputs, states, and protocol expectations.
- **Category-Partition**: Features are partitioned into logical input and behavior domains (e.g., valid keys vs. missing keys, valid checksums vs. corrupted payloads).
- **Boundary Value Analysis (BVA)**: Focuses on boundary limits such as maximum connection capacities, timeouts, zero/negative config bounds, empty IDs, and extreme payload sizes.
- **Pairwise Combinatorial Testing**: Evaluates interactions between orthogonal features (e.g., authentication failures during task execution, network drops during results reporting, configuration changes during active worker routing).
- **Workload Testing**: Exercises the system under high concurrency, cascading transient failures, dynamic scale-up/down, and end-to-end multi-stage pipeline workloads.

## 2. Feature Inventory
The E2E Test Suite targets six core features of the Distributed Agent Network:
1. **Worker Registration and Heartbeats**: Establishing connections, maintaining active worker pools, and periodic heartbeat checks to detect node liveness.
2. **API Authentication and Checksums**: Verifying message authenticity using `X-API-Key` headers and payload integrity using `X-Payload-SHA256` SHA-256 signatures.
3. **Async Task Processing & Execution**: Spawning asynchronous tasks, executing operations, and handling result reporting with appropriate state changes.
4. **Routing & Dispatch (Orchestrator Polling & Routing)**: Finding matching workers based on requested capability roles, managing queues when workers are busy, and polling statuses.
5. **Resilient HTTP & Connection Retries**: Mitigating network latency, transient errors (HTTP 429/503), drops, and backoff retry mechanisms.
6. **Workspace & Configuration Management**: Updating limits dynamically, worker deregistration, cleanup of dead nodes, and configuration boundary safety.

## 3. Test Architecture
The test infrastructure uses:
- **Pytest & Pytest-asyncio**: The standard asynchronous Python testing framework.
- **Production ClientWorker (`ag_core/distributed/worker.py`)**: The actual production class implementing the worker loop, heartbeat task, and task execution logic. Imported directly into tests and production scripts.
- **Production CentralHub (`ag_core/distributed/hub.py`)**: The actual production coordinator class managing workers, task queues, routing, API auth, payload checksums, and the background sweeper loop.
- **MockNetworkProtocol (`tests/test_distributed.py`)**: Simulates the transport layer, allowing precise injection of latency, dropped packets, and HTTP error statuses (like 429/503/401/400).

The business logic of `CentralHub` and `ClientWorker` executes exactly as it does in production, but is routed via the mock network protocol in tests to enable deterministic fault injection and timing controls without binding physical sockets.

## 4. Coverage Thresholds
The suite executes exactly **71 test cases** divided across 4 tiers:
- **Tier 1 (Feature Coverage)**: 30 tests (5 tests per feature for all 6 features)
- **Tier 2 (Boundary & Corner Cases)**: 30 tests (5 tests per feature for all 6 features)
- **Tier 3 (Cross-Feature Combinations)**: 6 tests (Pairwise interaction scenarios)
- **Tier 4 (Real-World Workloads)**: 5 tests (E2E workflows, concurrency, network chaos, dynamic scaling, crash recovery)

---
*MANDATORY INTEGRITY WARNING:*
*DO NOT CHEAT. All implementations must be genuine. DO NOT hardcode test results, create dummy/facade implementations, or circumvent the intended task. A Forensic Auditor will independently verify your work.*
