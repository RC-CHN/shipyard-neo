#!/bin/bash
#
# Run a single E2E test for quick debugging.
#
# Usage:
#   ./tests/scripts/run_single_test.sh [test_path]
#
# Example:
#   ./tests/scripts/run_single_test.sh tests/integration/test_extend_ttl.py::TestE2EExtendTTL::test_extend_ttl_rejects_expired
#
# Default: runs the extend_ttl_rejects_expired test

set -e

cd "$(dirname "$0")/../.."

# Default test to run
TEST_PATH="${1:-tests/integration/test_extend_ttl.py::TestE2EExtendTTL::test_extend_ttl_rejects_expired}"

# Check if Bay server is already running
BAY_PID=""
if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/health 2>/dev/null | grep -q "200"; then
    echo "[INFO] Bay server already running"
else
    echo "[INFO] Starting Bay server..."
    rm -f bay-e2e-test.db
    BAY_CONFIG_FILE=tests/scripts/docker-host/config.yaml uv run python -m app.main &
    BAY_PID=$!
    
    # Wait for server to be ready
    echo "[INFO] Waiting for Bay server to be ready..."
    for i in {1..30}; do
        if curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:8001/health 2>/dev/null | grep -q "200"; then
            echo "[INFO] Bay server is ready"
            break
        fi
        sleep 1
    done
fi

# Run the test
echo "[INFO] Running test: $TEST_PATH"
echo "============================================="
uv run pytest "$TEST_PATH" -v -s --tb=short

# Cleanup
if [ -n "$BAY_PID" ]; then
    echo "[INFO] Stopping Bay server (PID: $BAY_PID)"
    kill $BAY_PID 2>/dev/null || true
    wait $BAY_PID 2>/dev/null || true
fi
