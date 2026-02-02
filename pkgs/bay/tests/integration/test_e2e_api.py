"""E2E API tests for Bay - Entry Point.

This module serves as the entry point for all E2E integration tests.
The actual tests have been organized into separate modules for better
maintainability:

Test Modules (Core API):
- test_auth.py: E2E-00 Authentication tests
- test_minimal_path.py: E2E-01 Minimal path (create → python/exec)
- test_stop.py: E2E-02 Stop (reclaim compute only)
- test_delete.py: E2E-03 Delete (complete destruction)
- test_concurrent.py: E2E-04 Concurrent ensure_running
- test_file_transfer.py: E2E-05 File upload and download
- test_filesystem.py: E2E-06 Filesystem operations
- test_idempotency_e2e.py: E2E-07 Idempotency-Key support
- test_extend_ttl.py: E2E-XX Sandbox TTL extension (extend_ttl)
- test_capability_enforcement.py: E2E-12 Profile-level capability enforcement
- test_path_security.py: E2E-15 Path security validation
- test_shell_e2e.py: E2E-16 Shell execution functionality
- test_container_isolation.py: E2E-17 Container isolation verification (Scenario 7 Part B)
- test_shell_devops_workflow.py: E2E-18 Shell-driven DevOps automation (Scenario 8)
- test_mega_workflow.py: E2E-19 Mega workflow integration (Scenario 9)
- test_gc_e2e.py: E2E-GC Garbage Collection end-to-end tests
- test_gc_workflow_scenario.py: E2E-GC-Workflow Long GC chaos workflow scenario

Workflow Scenario Tests (from e2e-workflow-scenarios.md):
- test_interactive_workflow.py: E2E-08 Interactive Data Analysis (Scenario 1)
- test_script_development.py: E2E-09 Script Development and Debugging (Scenario 2)
- test_project_init.py: E2E-10 Project Initialization and Dependencies (Scenario 3)
- test_serverless_execution.py: E2E-11 Simple Quick Execution (Scenario 4)
- test_long_running_extend_ttl.py: E2E-13 Long Running Task with TTL Extension (Scenario 5)
- test_agent_coding_workflow.py: E2E-14 AI Agent Code Generation and Iterative Fix (Scenario 6)
- test_container_isolation.py: E2E-17 Container Isolation Verification (Scenario 7)
- test_shell_devops_workflow.py: E2E-18 Shell-driven DevOps Automation (Scenario 8)

Prerequisites:
- Docker daemon running and accessible
- ship:latest image built and available
- Bay server running on http://localhost:8000

See: plans/phase-1/tests.md section 1
See: plans/phase-1/e2e-workflow-scenarios.md for workflow scenarios
See: plans/phase-1/profile-capability-enforcement.md for capability enforcement
See: plans/phase-1.5/path-security-validation.md for path security
"""

from __future__ import annotations

# Re-export all test classes for backward compatibility
from .test_auth import TestE2E00Auth
from .test_minimal_path import TestE2E01MinimalPath
from .test_stop import TestE2E02Stop
from .test_delete import TestE2E03Delete
from .test_concurrent import TestE2E04ConcurrentEnsureRunning
from .test_file_transfer import TestE2E05FileUploadDownload
from .test_filesystem import TestE2E06Filesystem
from .test_idempotency_e2e import TestE2E07Idempotency
from .test_extend_ttl import TestE2EExtendTTL
from .test_interactive_workflow import TestE2E08InteractiveDataAnalysis
from .test_script_development import TestE2E09ScriptDevelopment
from .test_project_init import TestE2E10ProjectInitialization
from .test_serverless_execution import TestE2E11ServerlessExecution
from .test_long_running_extend_ttl import TestE2E13LongRunningExtendTTL
from .test_agent_coding_workflow import TestE2E14AgentCodingWorkflow
from .test_capability_enforcement import (
    TestCapabilityEnforcementE2E,
    TestFullProfileAllowsAll,
)
from .test_path_security import TestPathSecurityE2E
from .test_shell_e2e import TestShellExecE2E, TestShellExecSecurityE2E
from .test_container_isolation import TestContainerIsolationE2E
from .test_shell_devops_workflow import (
    TestShellDevOpsWorkflowE2E,
    TestNodeJsProjectBuildE2E,
    TestGitWorkflowE2E,
    TestShellPipeAndTextProcessingE2E,
    TestShellErrorHandlingE2E,
    TestPackageAndDownloadE2E,
    TestShellWorkingDirectoryE2E,
)
from .test_mega_workflow import TestMegaWorkflowE2E
from .test_gc_e2e import TestE2EGC
from .test_gc_workflow_scenario import TestE2EGCWorkflowScenario

# Re-export shared configuration for convenience
from .conftest import (
    BAY_BASE_URL,
    E2E_API_KEY,
    AUTH_HEADERS,
    DEFAULT_PROFILE,
    is_bay_running,
    is_docker_available,
    is_ship_image_available,
    docker_volume_exists,
    docker_container_exists,
)

__all__ = [
    # Test classes - Core API
    "TestE2E00Auth",
    "TestE2E01MinimalPath",
    "TestE2E02Stop",
    "TestE2E03Delete",
    "TestE2E04ConcurrentEnsureRunning",
    "TestE2E05FileUploadDownload",
    "TestE2E06Filesystem",
    "TestE2E07Idempotency",
    "TestE2EExtendTTL",
    # Test classes - Workflow Scenarios
    "TestE2E08InteractiveDataAnalysis",
    "TestE2E09ScriptDevelopment",
    "TestE2E10ProjectInitialization",
    "TestE2E11ServerlessExecution",
    "TestE2E13LongRunningExtendTTL",
    "TestE2E14AgentCodingWorkflow",
    # Test classes - Capability Enforcement
    "TestCapabilityEnforcementE2E",
    "TestFullProfileAllowsAll",
    # Test classes - Path Security
    "TestPathSecurityE2E",
    # Test classes - Shell Execution
    "TestShellExecE2E",
    "TestShellExecSecurityE2E",
    # Test classes - Container Isolation (Scenario 7)
    "TestContainerIsolationE2E",
    # Test classes - Shell DevOps Workflow (Scenario 8)
    "TestShellDevOpsWorkflowE2E",
    "TestNodeJsProjectBuildE2E",
    "TestGitWorkflowE2E",
    "TestShellPipeAndTextProcessingE2E",
    "TestShellErrorHandlingE2E",
    "TestPackageAndDownloadE2E",
    "TestShellWorkingDirectoryE2E",
    # Test classes - Mega Workflow (Scenario 9)
    "TestMegaWorkflowE2E",
    # Test classes - GC
    "TestE2EGC",
    "TestE2EGCWorkflowScenario",
    # Configuration
    "BAY_BASE_URL",
    "E2E_API_KEY",
    "AUTH_HEADERS",
    "DEFAULT_PROFILE",
    # Helper functions
    "is_bay_running",
    "is_docker_available",
    "is_ship_image_available",
    "docker_volume_exists",
    "docker_container_exists",
]
