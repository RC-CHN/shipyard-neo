#!/bin/bash
# Start Bay dev server for debugging
#
# Container NOT deleted after sandbox deletion (for log inspection)
# Always rebuilds ship and gull images to ensure latest code
#
# Usage:
#   ./start.sh              # Rebuild ship & gull images and start Bay dev server
#   ./start.sh --no-build   # Skip image rebuilds

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BAY_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
SHIP_DIR="$(cd "${BAY_DIR}/../ship" && pwd)"
GULL_DIR="$(cd "${BAY_DIR}/../gull" && pwd)"
CONFIG_FILE="${SCRIPT_DIR}/config.yaml"
BAY_PORT=8002

# Colors
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[WARN]${NC} $1"; }
log_error() { echo -e "${RED}[ERROR]${NC} $1"; }

check_prerequisites() {
    if ! docker info >/dev/null 2>&1; then
        log_error "Docker is not running"
        exit 1
    fi
}

build_ship_image() {
    log_info "Building ship:latest image..."
    cd "$SHIP_DIR"
    docker build -t ship:latest .
    cd "$BAY_DIR"
    log_info "✓ Ship image built successfully"
}

build_gull_image() {
    if [ -d "$GULL_DIR" ]; then
        log_info "Building gull:latest image..."
        cd "$GULL_DIR"
        docker build -t gull:latest .
        cd "$BAY_DIR"
        log_info "✓ Gull image built successfully"
    else
        log_warn "Gull directory not found at $GULL_DIR, skipping gull image build"
        log_warn "Browser capability will not be available"
    fi
}

# Parse arguments - default is to build
SKIP_BUILD=false
while [[ $# -gt 0 ]]; do
    case $1 in
        --no-build)
            SKIP_BUILD=true
            shift
            ;;
        *)
            log_error "Unknown option: $1"
            exit 1
            ;;
    esac
done

check_prerequisites

if [ "$SKIP_BUILD" = false ]; then
    build_ship_image
    build_gull_image
else
    log_info "Skipping image builds (--no-build)"
    if ! docker image inspect ship:latest >/dev/null 2>&1; then
        log_error "ship:latest image not found! Remove --no-build to build it."
        exit 1
    fi
    if ! docker image inspect gull:latest >/dev/null 2>&1; then
        log_warn "gull:latest image not found. Browser capability will not be available."
    fi
fi

log_info "Starting Bay dev server on port $BAY_PORT..."
cd "$BAY_DIR"
export BAY_CONFIG_FILE="$CONFIG_FILE"
uv run python -m app.main
