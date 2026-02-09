#!/usr/bin/env bash
# Run Gull integration tests (requires Docker + gull:latest image).
#
# Usage: cd pkgs/gull && ./tests/scripts/run_integration.sh
#
# Prerequisites:
#   docker build -t gull:latest -f Dockerfile .
set -euo pipefail

cd "$(dirname "$0")/../.."

CONTAINER_NAME="gull-integration-test"

# Ensure cleanup on exit (even on failure / Ctrl-C)
cleanup() {
    echo ""
    echo "=== Cleaning up ==="
    docker rm -f "$CONTAINER_NAME" 2>/dev/null && echo "Removed container: $CONTAINER_NAME" || true
}
trap cleanup EXIT

# Check prerequisites
if ! command -v docker &>/dev/null; then
    echo "ERROR: Docker CLI not found" >&2
    exit 1
fi

if ! docker image inspect gull:latest &>/dev/null; then
    echo "ERROR: gull:latest image not found. Build it first:" >&2
    echo "  docker build -t gull:latest -f Dockerfile ." >&2
    exit 1
fi

# Remove any leftover container from a previous crashed run
docker rm -f "$CONTAINER_NAME" 2>/dev/null || true

echo "=== Gull integration tests ==="
uv run pytest tests/integration/ -v --tb=short "$@"
echo ""
echo "=== All tests passed ==="
