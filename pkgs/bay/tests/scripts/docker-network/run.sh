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
#   ./run.sh --parallel   # Run tests in parallel (faster)
#   ./run.sh -v           # Verbose mode
#   ./run.sh -k "test_create"  # Run specific test
#   ./run.sh --parallel -n 4   # Run in parallel with 4 workers

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BAY_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yaml"
BAY_PORT=8002
NETWORK_NAME="bay-e2e-test-network"

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
    
    # Stop Bay container
    log_info "Stopping Bay container..."
    cd "$SCRIPT_DIR"
    docker-compose -f "$COMPOSE_FILE" down 2>/dev/null || true
    
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
    
    # Check docker-compose
    if ! docker-compose version >/dev/null 2>&1; then
        log_error "docker-compose is not installed"
        exit 1
    fi
    log_info "✓ docker-compose is available"
    
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
    
    # Start Bay with docker-compose (network will be created automatically)
    docker-compose -f "$COMPOSE_FILE" up -d --build
    
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
        if ! docker-compose -f "$COMPOSE_FILE" ps | grep -q "Up"; then
            log_error "Bay container exited unexpectedly"
            docker-compose -f "$COMPOSE_FILE" logs
            exit 1
        fi
        
        sleep 1
        attempt=$((attempt + 1))
    done
    
    log_error "Bay failed to start within ${max_attempts} seconds"
    docker-compose -f "$COMPOSE_FILE" logs
    exit 1
}

run_tests() {
    local parallel_mode="$1"
    shift
    
    log_info "Running E2E tests (docker-network mode)..."
    
    cd "$BAY_DIR"
    
    if [ "$parallel_mode" = "true" ]; then
        log_info "Running tests in parallel mode with pytest-xdist"
        # Use -n auto for automatic worker detection, --dist loadgroup for xdist_group support
        uv run pytest tests/integration/test_e2e_api.py -n auto --dist loadgroup "$@"
    else
        log_info "Running tests in serial mode"
        uv run pytest tests/integration/test_e2e_api.py "$@"
    fi
}

# Trap for cleanup on exit
trap cleanup EXIT

# Parse arguments
PYTEST_ARGS=""
PARALLEL_MODE="false"
while [[ $# -gt 0 ]]; do
    case $1 in
        --parallel)
            PARALLEL_MODE="true"
            shift
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

run_tests "$PARALLEL_MODE" $PYTEST_ARGS

log_info "E2E tests completed successfully!"
