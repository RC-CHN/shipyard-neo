#!/bin/bash
# Run Bay E2E tests with Docker container-network mode
#
# In this mode:
# - Bay runs in a Docker container
# - Ship containers are created and connected to the same network
# - Bay connects to Ship via container network IP (not host port mapping)
#
# Prerequisites:
# - Docker daemon running
# - ship:latest image built (cd pkgs/ship && make build)
# - bay:latest image built (cd pkgs/bay && make build)
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
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yaml"
BAY_PORT=8002
NETWORK_NAME="bay-e2e-test-network"

# Docker Compose command (supports both Compose v2: `docker compose` and legacy: `docker-compose`)
# Stored as an argv array, e.g. (docker compose) or (docker-compose)
COMPOSE_CMD=()

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

# Pick a compose implementation:
# - Prefer Compose v2: `docker compose`
# - Fallback to legacy: `docker-compose`
# This avoids forcing users to install the legacy `docker-compose` package.
detect_compose() {
    if docker compose version >/dev/null 2>&1; then
        COMPOSE_CMD=(docker compose)
        return 0
    fi

    if command -v docker-compose >/dev/null 2>&1; then
        COMPOSE_CMD=(docker-compose)
        return 0
    fi

    return 1
}

compose() {
    # Ensure COMPOSE_CMD is initialized (important for cleanup path)
    if [ ${#COMPOSE_CMD[@]} -eq 0 ]; then
        detect_compose || return 1
    fi

    "${COMPOSE_CMD[@]}" "$@"
}

cleanup() {
    log_info "Cleaning up..."
    
    # Stop Bay container
    log_info "Stopping Bay container..."
    cd "$SCRIPT_DIR"
    compose -f "$COMPOSE_FILE" down 2>/dev/null || true
    
    # Clean up any leftover test containers
    log_info "Cleaning up test containers..."
    docker ps -a --filter "label=bay.owner=e2e-test-user" -q 2>/dev/null | xargs -r docker rm -f 2>/dev/null || true
    
    # Clean up any leftover test volumes
    log_info "Cleaning up test volumes..."
    docker volume ls --filter "label=bay.owner=e2e-test-user" -q 2>/dev/null | xargs -r docker volume rm 2>/dev/null || true
    
    # Remove network if empty
    log_info "Cleaning up test network..."
    docker network rm "$NETWORK_NAME" 2>/dev/null || true
    
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
    
    # Check Docker Compose (v2 plugin or legacy)
    if ! detect_compose; then
        log_error "Docker Compose is not available (need either 'docker compose' or 'docker-compose')"
        exit 1
    fi
    log_info "✓ Docker Compose is available (${COMPOSE_CMD[*]})"
    
    # Check compose file
    if [ ! -f "$COMPOSE_FILE" ]; then
        log_error "docker-compose.yaml not found: $COMPOSE_FILE"
        exit 1
    fi
    log_info "✓ docker-compose.yaml exists"
    
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
    
    log_info "Building bay:latest image..."
    
    cd "$BAY_DIR"
    docker build -t bay:latest .
    
    log_info "✓ bay:latest image built"
}

start_bay_container() {
    log_info "Starting Bay container (docker-network mode)..."
    
    cd "$SCRIPT_DIR"
    
    # Start Bay with Docker Compose (network will be created automatically)
    compose -f "$COMPOSE_FILE" up -d --build
    
    log_info "Bay container started"
    
    # Wait for Bay to be ready
    log_info "Waiting for Bay to be ready..."
    local max_attempts=60
    local attempt=1
    
    while [ $attempt -le $max_attempts ]; do
        if curl -s "http://127.0.0.1:$BAY_PORT/health" >/dev/null 2>&1; then
            log_info "✓ Bay is ready"
            return 0
        fi
        
        # Check if container is still running
        if ! compose -f "$COMPOSE_FILE" ps | grep -q "Up"; then
            log_error "Bay container exited unexpectedly"
            compose -f "$COMPOSE_FILE" logs
            exit 1
        fi
        
        sleep 1
        attempt=$((attempt + 1))
    done
    
    log_error "Bay failed to start within ${max_attempts} seconds"
    compose -f "$COMPOSE_FILE" logs
    exit 1
}

run_tests() {
    local parallel_mode="$1"
    local num_workers="$2"
    shift 2

    log_info "Running E2E tests (docker-network mode)..."

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
start_bay_container

# Set environment variable for tests to know which port to use
export E2E_BAY_PORT=$BAY_PORT

run_tests "$PARALLEL_MODE" "$NUM_WORKERS" $PYTEST_ARGS

log_info "E2E tests completed successfully!"
