"""Integration/E2E tests for Bay.

Core API Test Modules:
- test_auth: E2E-00 Authentication
- test_minimal_path: E2E-01 Minimal path (create → python/exec)
- test_stop: E2E-02 Stop (reclaim compute only)
- test_delete: E2E-03 Delete (complete destruction)
- test_concurrent: E2E-04 Concurrent ensure_running
- test_file_transfer: E2E-05 File upload and download
- test_filesystem: E2E-06 Filesystem operations
- test_idempotency_e2e: E2E-07 Idempotency-Key support
- test_capability_enforcement: E2E-12 Profile-level capability enforcement

Workflow Scenario Tests (from e2e-workflow-scenarios.md):
- test_interactive_workflow: E2E-08 Interactive Data Analysis (Scenario 1)
- test_script_development: E2E-09 Script Development and Debugging (Scenario 2)
- test_project_init: E2E-10 Project Initialization and Dependencies (Scenario 3)
- test_serverless_execution: E2E-11 Simple Quick Execution (Scenario 4)

Configuration:
- conftest: Shared fixtures and helper functions

Legacy Entry Point:
- test_e2e_api: Re-exports all test classes for backward compatibility
"""
