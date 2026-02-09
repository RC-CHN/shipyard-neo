#!/usr/bin/env bash
# Run Gull unit tests (no Docker required).
# Usage: cd pkgs/gull && ./tests/scripts/run_unit.sh
set -euo pipefail

cd "$(dirname "$0")/../.."

echo "=== Gull unit tests ==="
uv run pytest tests/unit/ -v "$@"
