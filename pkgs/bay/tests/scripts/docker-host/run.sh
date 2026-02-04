#!/bin/bash
# Run Bay E2E tests with Docker host-port mode
#
# Prerequisites:
# - Docker daemon running
# - ship:latest image built (cd pkgs/ship && make build)
#
# Usage:
#   ./run.sh              # Run all E2E tests (serial)
#   ./run.sh --parallel   # Run tests in parallel (auto workers)
#   ./run.sh --parallel -n 4  # Run with 4 workers
#   ./run.sh -v           # Verbose mode
#   ./run.sh -k "test_create"  # Run specific test

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BAY_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/config.yaml"
DB_FILE="${BAY_DIR}/bay-e2e-test.db"
BAY_PORT=8001
BAY_PID=""

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

log_info() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

log_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

cleanup() {
    log_info "Cleaning up..."
    
    # Stop Bay server
    if [ -n "$BAY_PID" ]; then
        log_info "Stopping Bay server (PID: $BAY_PID)"
        kill $BAY_PID 2>/dev/null || true
        wait $BAY_PID 2>/dev/null || true
    fi
    
    # Clean up test database
    if [ -f "$DB_FILE" ]; then
        log_info "Removing test database"
        rm -f "$DB_FILE"
    fi
    
    # Clean up any leftover test containers
    log_info "Cleaning up test containers..."
    docker ps -a --filter "label=bay.owner=e2e-test-user" -q 2>/dev/null | xargs -r docker rm -f 2>/dev/null || true
    
    # Clean up any leftover test volumes
    log_info "Cleaning up test volumes..."
    docker volume ls --filter "label=bay.owner=e2e-test-user" -q 2>/dev/null | xargs -r docker volume rm 2>/dev/null || true
    
    log_info "Cleanup complete"
}

check_prerequisites() {
    log_info "Checking prerequisites..."
    
    # Check Docker
    if ! docker info >/dev/null 2>&1; then
        log_error "Docker is not running or not accessible"
        exit 1
    fi
    log_info "✓ Docker is available"
    
    # Check config file
    if [ ! -f "$CONFIG_FILE" ]; then
        log_error "Config file not found: $CONFIG_FILE"
        exit 1
    fi
    log_info "✓ Config file exists"
    
    # Check port is available
    if command -v lsof >/dev/null 2>&1; then
        if lsof -i :$BAY_PORT >/dev/null 2>&1; then
            log_error "Port $BAY_PORT is already in use"
            exit 1
        fi
    fi
    log_info "✓ Port $BAY_PORT is available"
}

build_images() {
    log_info "Building ship:latest image..."
    
    SHIP_DIR="$(cd "${BAY_DIR}/../ship" && pwd)"
    
    if [ ! -d "$SHIP_DIR" ]; then
        log_error "Ship directory not found: $SHIP_DIR"
        exit 1
    fi
    
    cd "$SHIP_DIR"
    docker build -t ship:latest .
    
    log_info "✓ ship:latest image built"
}

start_bay_server() {
    log_info "Starting Bay server on port $BAY_PORT (docker-host mode)..."
    
    cd "$BAY_DIR"
    
    # Set environment variable to use test config
    export BAY_CONFIG_FILE="$CONFIG_FILE"
    
    # Start Bay in background
    uv run python -m app.main &
    BAY_PID=$!
    
    log_info "Bay server started (PID: $BAY_PID)"
    
    # Wait for Bay to be ready
    log_info "Waiting for Bay to be ready..."
    local max_attempts=30
    local attempt=1
    
    while [ $attempt -le $max_attempts ]; do
        if curl -s "http://127.0.0.1:$BAY_PORT/health" >/dev/null 2>&1; then
            log_info "✓ Bay is ready"
            return 0
        fi
        
        # Check if process is still running
        if ! kill -0 $BAY_PID 2>/dev/null; then
            log_error "Bay server exited unexpectedly"
            exit 1
        fi
        
        sleep 1
        attempt=$((attempt + 1))
    done
    
    log_error "Bay failed to start within ${max_attempts} seconds"
    exit 1
}

run_tests() {
    local parallel_mode="$1"
    local num_workers="$2"
    shift 2

    log_info "Running E2E tests (docker-host mode)..."

    cd "$BAY_DIR"

    # IMPORTANT:
    # - Phase 1: parallel, exclude serial tests
    # - Phase 2: exclusive, run serial tests with -n 1
    # This avoids GC/workflow/timing tests overlapping with other tests.
    if [ "$parallel_mode" = "true" ]; then
        log_info "Running tests in two-phase mode (parallel + exclusive serial)"

        log_info "Phase 1: parallel (not serial) with -n ${num_workers}"
        uv run pytest tests/integration -n "$num_workers" --dist loadgroup -m "not serial" "$@"

        log_info "Phase 2: exclusive serial (-n 1)"
        uv run pytest tests/integration -n 1 -m "serial" "$@"
    else
        log_info "Running tests in serial mode"
        uv run pytest tests/integration "$@"
    fi
}

# Trap for cleanup on exit
trap cleanup EXIT

# Parse arguments
PYTEST_ARGS=""
PARALLEL_MODE="false"
NUM_WORKERS="auto"
while [[ $# -gt 0 ]]; do
    case $1 in
        --parallel)
            PARALLEL_MODE="true"
            shift
            ;;
        -n)
            NUM_WORKERS="$2"
            shift 2
            ;;
        *)
            PYTEST_ARGS="$PYTEST_ARGS $1"
            shift
            ;;
    esac
done

# Main execution
check_prerequisites
build_images
start_bay_server

# Set environment variable for tests to know which port to use
export E2E_BAY_PORT=$BAY_PORT

run_tests "$PARALLEL_MODE" "$NUM_WORKERS" $PYTEST_ARGS

log_info "E2E tests completed successfully!"
