#!/bin/bash
#
# Run a single E2E/integration test against Bay (K8s/kind mode).
#
# This script reuses the existing Bay Pod + port-forward from run.sh.
# If Bay is not running on the expected port, it will fail fast.
#
# Usage:
#   ./run_single_test.sh [nodeid] [-- <pytest-args...>]
#
# Examples:
#   ./run_single_test.sh
#   ./run_single_test.sh tests/integration/core/test_auth.py::test_auth
#   ./run_single_test.sh tests/integration/workflows/test_serverless_execution.py::TestServerlessExecutionWorkflow::test_delete_cleans_up_all_resources -- -vv -s
#
# Default (no args): run the *first collected* test from tests/integration/core/test_auth.py
#
# NOTE: Before using this, make sure the Bay Pod is deployed and port-forwarded.
# You can do this by running run.sh first (it will fail at the end since it
# tries to run all tests, but the Bay Pod will be up). Or use the following
# manual steps:
#
#   1. Run the full setup from run.sh (up to port-forward)
#   2. Keep the port-forward running in a terminal
#   3. Run this script in another terminal
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BAY_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"  # pkgs/bay
BAY_PORT="${E2E_BAY_PORT:-8003}"
NAMESPACE="${E2E_K8S_NAMESPACE:-bay-e2e-test}"

cd "$BAY_DIR"

log() {
  echo "[kind][single] $*"
}

bay_is_running() {
  curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${BAY_PORT}/health" 2>/dev/null | grep -q "200"
}

first_core_auth_nodeid() {
  uv run pytest -q --collect-only tests/integration/core/test_auth.py \
    | sed -n 's/^\(tests\/integration\/core\/test_auth\.py::.*\)$/\1/p' \
    | head -n 1
}

parse_args() {
  NODEID=""
  PYTEST_ARGS=("-v" "-s" "--tb=short")

  if [ $# -gt 0 ] && [ "$1" != "--" ]; then
    NODEID="$1"
    shift
  fi

  if [ $# -gt 0 ] && [ "$1" == "--" ]; then
    shift
    while [ $# -gt 0 ]; do
      PYTEST_ARGS+=("$1")
      shift
    done
  fi

  if [ -z "$NODEID" ]; then
    NODEID="$(first_core_auth_nodeid)"
    if [ -z "$NODEID" ]; then
      echo "[kind][single][ERROR] failed to determine default nodeid from core/test_auth.py" >&2
      exit 1
    fi
  fi

  # Set environment variables for K8s mode
  export E2E_BAY_PORT="$BAY_PORT"
  export E2E_DRIVER_TYPE="k8s"
  export E2E_K8S_NAMESPACE="$NAMESPACE"

  log "Running: ${NODEID}"
  uv run pytest "$NODEID" "${PYTEST_ARGS[@]}"
}

# Check if Bay is running
if ! bay_is_running; then
  echo "[kind][single][ERROR] Bay is not running on port ${BAY_PORT}." >&2
  echo "" >&2
  echo "Make sure the Bay Pod is deployed and port-forwarded." >&2
  echo "Options:" >&2
  echo "  1. Run ./run.sh first (Ctrl+C after 'Running E2E tests' to keep Bay up)" >&2
  echo "  2. Manual setup:" >&2
  echo "     kubectl port-forward pod/bay -n ${NAMESPACE} ${BAY_PORT}:8000 &" >&2
  exit 1
fi

log "Bay is running on port ${BAY_PORT}"
parse_args "$@"
