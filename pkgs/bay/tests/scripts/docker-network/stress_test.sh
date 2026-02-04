#!/bin/bash
# Bay Stress Test (Docker Network Mode) - Measure concurrent sandbox operations throughput
#
# This is NOT a pass/fail test - it measures system performance under load.
#
# Prerequisites:
# - Docker daemon running
# - ship:latest and bay:latest images built
#
# Usage:
#   ./stress_test.sh                    # 10 sandboxes, 5 concurrent
#   ./stress_test.sh -s 20 -c 10        # 20 sandboxes, 10 concurrent
#   ./stress_test.sh --skip-cleanup     # Don't delete sandboxes after

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BAY_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
COMPOSE_FILE="${SCRIPT_DIR}/docker-compose.yaml"
BAY_PORT=8002
NETWORK_NAME="bay-e2e-test-network"

# Docker Compose command (supports both Compose v2: `docker compose` and legacy: `docker-compose`)
# Stored as an argv array, e.g. (docker compose) or (docker-compose)
COMPOSE_CMD=()

# Default parameters
NUM_SANDBOXES=10
CONCURRENCY=5
SKIP_CLEANUP=false

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
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

log_perf() {
    echo -e "${CYAN}[PERF]${NC} $1"
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
    docker build -t ship:latest . >/dev/null 2>&1
    
    log_info "✓ ship:latest image built"
    
    log_info "Building bay:latest image..."
    
    cd "$BAY_DIR"
    docker build -t bay:latest . >/dev/null 2>&1
    
    log_info "✓ bay:latest image built"
}

start_bay_container() {
    log_info "Starting Bay container (docker-network mode)..."
    
    cd "$SCRIPT_DIR"
    
    # Start Bay with Docker Compose (network will be created automatically)
    compose -f "$COMPOSE_FILE" up -d --build >/dev/null 2>&1
    
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

run_stress_test() {
    log_info "Running stress test..."
    log_info "  Sandboxes: $NUM_SANDBOXES"
    log_info "  Concurrency: $CONCURRENCY"
    log_info "  Skip cleanup: $SKIP_CLEANUP"
    
    cd "$BAY_DIR"
    
    # Run the Python stress test
    E2E_BAY_PORT=$BAY_PORT uv run python -c "
import asyncio
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

BAY_BASE_URL = f\"http://127.0.0.1:{os.environ.get('E2E_BAY_PORT', '8002')}\"
AUTH_HEADERS = {'Authorization': 'Bearer e2e-test-api-key'}
DEFAULT_PROFILE = 'python-default'

@dataclass
class Stats:
    total_ops: int = 0
    successes: int = 0
    failures: int = 0
    timeouts: int = 0
    op_times: list = field(default_factory=list)

    def add(self, success: bool, duration: float, timeout: bool = False):
        self.total_ops += 1
        self.op_times.append(duration)
        if timeout:
            self.timeouts += 1
        elif success:
            self.successes += 1
        else:
            self.failures += 1

    @property
    def avg_time(self):
        return sum(self.op_times) / len(self.op_times) if self.op_times else 0

    @property
    def min_time(self):
        return min(self.op_times) if self.op_times else 0

    @property
    def max_time(self):
        return max(self.op_times) if self.op_times else 0

    @property
    def p50_time(self):
        if not self.op_times:
            return 0
        s = sorted(self.op_times)
        return s[len(s) // 2]

    @property
    def p99_time(self):
        if not self.op_times:
            return 0
        s = sorted(self.op_times)
        return s[min(int(len(s) * 0.99), len(s) - 1)]

async def create_sandbox(client):
    start = time.perf_counter()
    try:
        resp = await client.post('/v1/sandboxes', json={'profile': DEFAULT_PROFILE}, timeout=120.0)
        duration = time.perf_counter() - start
        return (resp.json()['id'], duration) if resp.status_code == 201 else (None, duration)
    except httpx.TimeoutException:
        return None, time.perf_counter() - start

async def exec_python(client, sandbox_id):
    start = time.perf_counter()
    try:
        resp = await client.post(f'/v1/sandboxes/{sandbox_id}/python/exec',
                                 json={'code': 'print(1+1)', 'timeout': 30}, timeout=120.0)
        return resp.status_code == 200, time.perf_counter() - start
    except httpx.TimeoutException:
        return False, time.perf_counter() - start

async def delete_sandbox(client, sandbox_id):
    start = time.perf_counter()
    try:
        resp = await client.delete(f'/v1/sandboxes/{sandbox_id}', timeout=120.0)
        return resp.status_code == 204, time.perf_counter() - start
    except httpx.TimeoutException:
        return False, time.perf_counter() - start

async def run_lifecycle(client, create_stats, exec_stats, delete_stats, skip_cleanup):
    sandbox_id, create_time = await create_sandbox(client)
    create_stats.add(sandbox_id is not None, create_time)
    if sandbox_id is None:
        return
    exec_ok, exec_time = await exec_python(client, sandbox_id)
    exec_stats.add(exec_ok, exec_time)
    if not skip_cleanup:
        del_ok, del_time = await delete_sandbox(client, sandbox_id)
        delete_stats.add(del_ok, del_time)

def print_stats(name, stats):
    if stats.total_ops == 0:
        print(f'  {name}: No operations')
        return
    print(f'  {name}:')
    print(f'    Total: {stats.total_ops}, Success: {stats.successes}, Fail: {stats.failures}, Timeout: {stats.timeouts}')
    print(f'    Avg: {stats.avg_time:.3f}s, Min: {stats.min_time:.3f}s, Max: {stats.max_time:.3f}s')
    print(f'    P50: {stats.p50_time:.3f}s, P99: {stats.p99_time:.3f}s')

async def main(num_sandboxes, concurrency, skip_cleanup):
    print(f'\\n{\"=\"*60}')
    print('Bay Stress Test Results (Docker Network Mode)')
    print(f'{\"=\"*60}')
    
    create_stats, exec_stats, delete_stats = Stats(), Stats(), Stats()
    semaphore = asyncio.Semaphore(concurrency)

    async def bounded(client):
        async with semaphore:
            await run_lifecycle(client, create_stats, exec_stats, delete_stats, skip_cleanup)

    start_time = time.perf_counter()
    async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS, timeout=120.0) as client:
        await asyncio.gather(*[bounded(client) for _ in range(num_sandboxes)], return_exceptions=True)
    total_time = time.perf_counter() - start_time

    print(f'Total time: {total_time:.2f}s')
    print(f'Throughput: {num_sandboxes / total_time:.2f} sandboxes/sec')
    print('\\nOperation Statistics:')
    print_stats('Create', create_stats)
    print_stats('Exec', exec_stats)
    if not skip_cleanup:
        print_stats('Delete', delete_stats)
    print(f'{\"=\"*60}\\n')

# Convert bash boolean to Python boolean
skip = True if '$SKIP_CLEANUP' == 'true' else False
asyncio.run(main($NUM_SANDBOXES, $CONCURRENCY, skip))
"
}

# Trap for cleanup on exit
trap cleanup EXIT

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        -s|--sandboxes)
            NUM_SANDBOXES="$2"
            shift 2
            ;;
        -c|--concurrent)
            CONCURRENCY="$2"
            shift 2
            ;;
        --skip-cleanup)
            SKIP_CLEANUP=true
            shift
            ;;
        -h|--help)
            echo "Usage: $0 [options]"
            echo ""
            echo "Options:"
            echo "  -s, --sandboxes N    Number of sandboxes to create (default: 10)"
            echo "  -c, --concurrent N   Max concurrent operations (default: 5)"
            echo "  --skip-cleanup       Don't delete sandboxes after test"
            echo "  -h, --help           Show this help"
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

# Main execution
check_prerequisites
build_images
start_bay_container
run_stress_test

log_info "Stress test completed!"
