#!/bin/bash
#
# Run a single E2E/integration test against Bay (K8s/kind mode).
#
# This script is self-contained: it detects the K8s backend, builds images,
# sets up namespace/RBAC/Pod/port-forward if Bay is not already running,
# runs the specified test, and cleans up afterwards.
#
# If Bay is already running (port-forward healthy), it skips setup and
# does NOT clean up on exit (to avoid destroying a shared environment).
#
# Usage:
#   ./run_single_test.sh [nodeid] [-- <pytest-args...>]
#
# Examples:
#   ./run_single_test.sh
#   ./run_single_test.sh tests/integration/core/test_auth.py::test_auth
#   ./run_single_test.sh tests/integration/workflows/test_browser_workflow.py -- -vv -s
#   ./run_single_test.sh tests/integration/workflows/test_serverless_execution.py::TestServerlessExecutionWorkflow::test_delete_cleans_up_all_resources -- -vv -s
#
# Default (no args): run the *first collected* test from tests/integration/core/test_auth.py
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BAY_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"  # pkgs/bay
BAY_PORT="${E2E_BAY_PORT:-8003}"
NAMESPACE="${E2E_K8S_NAMESPACE:-bay-e2e-test}"
KIND_CLUSTER_NAME="bay-e2e-test"
BACKEND=""
PORT_FORWARD_PID=""
SETUP_BY_US=false  # Track whether we created the environment

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

log_info() { echo -e "${GREEN}[kind][single][INFO]${NC} $1"; }
log_warn() { echo -e "${YELLOW}[kind][single][WARN]${NC} $1"; }
log_error() { echo -e "${RED}[kind][single][ERROR]${NC} $1"; }

cd "$BAY_DIR"

# ─── Cleanup ─────────────────────────────────────────────────────────
cleanup() {
    if [ "$SETUP_BY_US" != "true" ]; then
        log_info "Bay was already running; skipping cleanup"
        return 0
    fi

    log_info "Cleaning up..."

    # Stop port-forward if we started it
    if [ -n "$PORT_FORWARD_PID" ] && kill -0 "$PORT_FORWARD_PID" 2>/dev/null; then
        log_info "Stopping port-forward..."
        kill "$PORT_FORWARD_PID" 2>/dev/null || true
        wait "$PORT_FORWARD_PID" 2>/dev/null || true
    fi

    # Delete namespace (cleans up Pods, PVCs, etc.)
    if kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
        log_info "Deleting namespace $NAMESPACE..."
        kubectl delete namespace "$NAMESPACE" --grace-period=0 --force 2>/dev/null || true
        local max_wait=60 waited=0
        while kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 && [ $waited -lt $max_wait ]; do
            sleep 1
            waited=$((waited + 1))
        done
        if [ $waited -ge $max_wait ]; then
            log_warn "Namespace deletion timed out, may need manual cleanup"
        fi
    fi

    log_info "Cleanup complete"
}

trap cleanup EXIT

# ─── Backend detection ───────────────────────────────────────────────
detect_backend() {
    CURRENT_CONTEXT=$(kubectl config current-context 2>/dev/null || echo "")
    if [ -z "$CURRENT_CONTEXT" ]; then
        if command -v kind >/dev/null 2>&1; then
            BACKEND="kind"
            log_info "No kubectl context found, will use kind"
        else
            log_error "No kubectl context and kind not installed"
            exit 1
        fi
        return
    fi

    if [[ "$CURRENT_CONTEXT" == "docker-desktop" ]] || [[ "$CURRENT_CONTEXT" == "docker-for-desktop" ]]; then
        BACKEND="docker-desktop"
        log_info "Detected Docker Desktop Kubernetes"
        return
    fi

    if kubectl cluster-info >/dev/null 2>&1; then
        BACKEND="existing"
        log_info "Using existing cluster (context: $CURRENT_CONTEXT)"
        return
    fi

    if command -v kind >/dev/null 2>&1; then
        BACKEND="kind"
        log_info "Cannot connect to cluster, will use kind"
    else
        log_error "Cannot connect to cluster and kind not installed"
        exit 1
    fi
}

# ─── kind helpers ────────────────────────────────────────────────────
detect_kind_cluster_name() {
    local node_names
    node_names=$(kubectl get nodes -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || echo "")
    for node in $node_names; do
        if [[ "$node" == *"-control-plane" ]]; then
            local cluster_name="${node%-control-plane}"
            if command -v kind >/dev/null 2>&1 && kind get clusters 2>/dev/null | grep -q "^${cluster_name}$"; then
                echo "$cluster_name"
                return 0
            fi
            echo "$cluster_name"
            return 0
        fi
    done
    return 1
}

load_images_to_kind() {
    local cluster_name="$1"
    log_info "Loading images into kind cluster '$cluster_name'..."
    kind load docker-image ship:latest --name "$cluster_name"
    kind load docker-image bay:latest --name "$cluster_name"
    if docker image inspect gull:latest >/dev/null 2>&1; then
        kind load docker-image gull:latest --name "$cluster_name"
        log_info "✓ gull:latest loaded into kind cluster"
    fi
    log_info "✓ Images loaded into kind cluster"
}

# ─── Build images ────────────────────────────────────────────────────
build_images() {
    log_info "Building ship:latest image..."
    SHIP_DIR="$(cd "${BAY_DIR}/../ship" && pwd)"
    if [ ! -d "$SHIP_DIR" ]; then
        log_error "Ship directory not found: $SHIP_DIR"
        exit 1
    fi
    cd "$SHIP_DIR" && docker build -t ship:latest .
    log_info "✓ ship:latest image built"

    GULL_DIR="$(cd "${BAY_DIR}/../gull" 2>/dev/null && pwd)" || true
    if [ -n "$GULL_DIR" ] && [ -d "$GULL_DIR" ]; then
        log_info "Building gull:latest image..."
        cd "$GULL_DIR" && docker build -t gull:latest .
        log_info "✓ gull:latest image built"
    else
        log_warn "Gull directory not found - skipping gull:latest build (browser tests may be skipped)"
    fi

    log_info "Building bay:latest image..."
    cd "$BAY_DIR" && docker build -t bay:latest .
    log_info "✓ bay:latest image built"
}

# ─── Namespace + RBAC ────────────────────────────────────────────────
setup_namespace_and_rbac() {
    log_info "Setting up namespace and RBAC..."

    if kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
        log_warn "Namespace $NAMESPACE exists, deleting for clean slate..."
        kubectl delete namespace "$NAMESPACE" --grace-period=0 --force 2>/dev/null || true
        sleep 3
    fi

    kubectl create namespace "$NAMESPACE"
    log_info "✓ Created namespace $NAMESPACE"

    kubectl apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: bay
  namespace: $NAMESPACE
EOF

    kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: Role
metadata:
  name: bay-role
  namespace: $NAMESPACE
rules:
  - apiGroups: [""]
    resources: ["pods"]
    verbs: ["create", "delete", "get", "list", "watch"]
  - apiGroups: [""]
    resources: ["pods/log"]
    verbs: ["get"]
  - apiGroups: [""]
    resources: ["persistentvolumeclaims"]
    verbs: ["create", "delete", "get", "list", "watch"]
EOF

    kubectl apply -f - <<EOF
apiVersion: rbac.authorization.k8s.io/v1
kind: RoleBinding
metadata:
  name: bay-rolebinding
  namespace: $NAMESPACE
subjects:
  - kind: ServiceAccount
    name: bay
    namespace: $NAMESPACE
roleRef:
  kind: Role
  name: bay-role
  apiGroup: rbac.authorization.k8s.io
EOF

    log_info "✓ RBAC configured"
}

# ─── Deploy Bay Pod ──────────────────────────────────────────────────
deploy_bay_pod() {
    log_info "Deploying Bay Pod..."

    kubectl apply -f - <<EOF
apiVersion: v1
kind: ConfigMap
metadata:
  name: bay-config
  namespace: $NAMESPACE
data:
  config.yaml: |
    server:
      host: "0.0.0.0"
      port: 8114

    database:
      url: "sqlite+aiosqlite:///./bay-e2e-test.db"
      echo: false

    driver:
      type: k8s
      k8s:
        namespace: "$NAMESPACE"
        kubeconfig: null
        storage_class: null
        default_storage_size: "1Gi"
        image_pull_secrets: []
        pod_startup_timeout: 120
        label_prefix: "bay"

    cargo:
      root_path: "/var/lib/bay/cargos"
      default_size_limit_mb: 1024
      mount_path: "/workspace"

    security:
      api_key: "e2e-test-api-key"
      allow_anonymous: false

    profiles:
      - id: python-default
        image: "ship:latest"
        runtime_type: ship
        runtime_port: 8123
        resources:
          cpus: 1.0
          memory: "1g"
        capabilities:
          - filesystem
          - shell
          - python
          - upload
          - download
        idle_timeout: 300
        env: {}

      - id: python-only-test
        image: "ship:latest"
        runtime_type: ship
        runtime_port: 8123
        resources:
          cpus: 1.0
          memory: "1g"
        capabilities:
          - python
        idle_timeout: 300
        env: {}

      - id: short-idle-test
        image: "ship:latest"
        runtime_type: ship
        runtime_port: 8123
        resources:
          cpus: 1.0
          memory: "1g"
        capabilities:
          - filesystem
          - shell
          - python
        idle_timeout: 2
        env: {}

      - id: oom-test
        image: "ship:latest"
        runtime_type: ship
        runtime_port: 8123
        resources:
          cpus: 1.0
          memory: "128m"
        capabilities:
          - filesystem
          - shell
          - python
        idle_timeout: 300
        env: {}

      - id: browser-python
        description: "Browser automation with Python backend"
        containers:
          - name: ship
            image: "ship:latest"
            runtime_type: ship
            runtime_port: 8123
            resources:
              cpus: 1.0
              memory: "1g"
            capabilities:
              - python
              - shell
              - filesystem
            primary_for:
              - filesystem
              - python
              - shell
            env: {}
          - name: browser
            image: "gull:latest"
            runtime_type: gull
            runtime_port: 8115
            resources:
              cpus: 1.0
              memory: "2g"
            capabilities:
              - browser
            env: {}
        idle_timeout: 300

    gc:
      enabled: false
      run_on_startup: false
      interval_seconds: 300
      instance_id: "bay-k8s-e2e"
      idle_session:
        enabled: true
      expired_sandbox:
        enabled: true
      orphan_cargo:
        enabled: true
      orphan_container:
        enabled: true
EOF

    kubectl apply -f - <<EOF
apiVersion: v1
kind: Pod
metadata:
  name: bay
  namespace: $NAMESPACE
  labels:
    app: bay
spec:
  serviceAccountName: bay
  containers:
    - name: bay
      image: bay:latest
      imagePullPolicy: Never
      ports:
        - containerPort: 8000
      env:
        - name: BAY_CONFIG_FILE
          value: "/etc/bay/config.yaml"
      volumeMounts:
        - name: config
          mountPath: /etc/bay
      readinessProbe:
        httpGet:
          path: /health
          port: 8114
        initialDelaySeconds: 5
        periodSeconds: 2
      livenessProbe:
        httpGet:
          path: /health
          port: 8114
        initialDelaySeconds: 10
        periodSeconds: 10
  volumes:
    - name: config
      configMap:
        name: bay-config
  restartPolicy: Never
EOF

    log_info "✓ Bay Pod created"

    # Wait for Bay Pod to be ready
    log_info "Waiting for Bay Pod to be ready..."
    local max_wait=120 waited=0 interval=5
    while [ $waited -lt $max_wait ]; do
        POD_STATUS=$(kubectl get pod/bay -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
        CONTAINER_STATUS=$(kubectl get pod/bay -n "$NAMESPACE" -o jsonpath='{.status.containerStatuses[0].state}' 2>/dev/null | grep -oE '"[^"]+":' | head -1 | tr -d '":' || echo "unknown")

        if [ "$POD_STATUS" = "Running" ]; then
            READY=$(kubectl get pod/bay -n "$NAMESPACE" -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null || echo "false")
            if [ "$READY" = "true" ]; then
                log_info "✓ Bay Pod is ready"
                return 0
            fi
        fi

        log_info "  Pod status: $POD_STATUS, Container: $CONTAINER_STATUS (waited ${waited}s / ${max_wait}s)"

        if [[ "$CONTAINER_STATUS" == *"ImagePull"* ]] || [[ "$CONTAINER_STATUS" == *"ErrImage"* ]]; then
            log_error "Pod is stuck trying to pull image"
            kubectl describe pod/bay -n "$NAMESPACE" | tail -20
            exit 1
        fi

        if [ "$POD_STATUS" = "Failed" ]; then
            log_error "Pod failed to start"
            kubectl logs pod/bay -n "$NAMESPACE" 2>/dev/null || true
            kubectl describe pod/bay -n "$NAMESPACE" | tail -20
            exit 1
        fi

        sleep $interval
        waited=$((waited + interval))
    done

    log_error "Timeout waiting for Bay Pod to be ready"
    kubectl describe pod/bay -n "$NAMESPACE" | tail -30
    exit 1
}

# ─── Port forward ────────────────────────────────────────────────────
start_port_forward() {
    log_info "Starting port-forward to Bay Pod..."
    kubectl port-forward pod/bay -n "$NAMESPACE" $BAY_PORT:8114 &
    PORT_FORWARD_PID=$!
    sleep 2

    local max_attempts=30 attempt=1
    while [ $attempt -le $max_attempts ]; do
        if curl -s "http://127.0.0.1:$BAY_PORT/health" >/dev/null 2>&1; then
            log_info "✓ Port-forward is ready"
            return 0
        fi
        if ! kill -0 "$PORT_FORWARD_PID" 2>/dev/null; then
            log_error "Port-forward process exited unexpectedly"
            exit 1
        fi
        sleep 1
        attempt=$((attempt + 1))
    done

    log_error "Port-forward failed to become ready"
    exit 1
}

# ─── Test health check ───────────────────────────────────────────────
bay_is_running() {
    curl -s -o /dev/null -w "%{http_code}" "http://127.0.0.1:${BAY_PORT}/health" 2>/dev/null | grep -q "200"
}

# ─── Nodeid helper ───────────────────────────────────────────────────
first_core_auth_nodeid() {
    uv run pytest -q --collect-only tests/integration/core/test_auth.py \
        | sed -n 's/^\(tests\/integration\/core\/test_auth\.py::.*\)$/\1/p' \
        | head -n 1
}

# ─── Parse CLI args ──────────────────────────────────────────────────
NODEID=""
PYTEST_ARGS=("-v" "-s" "--tb=short")

while [ $# -gt 0 ]; do
    if [ "$1" = "--" ]; then
        shift
        while [ $# -gt 0 ]; do
            PYTEST_ARGS+=("$1")
            shift
        done
        break
    fi
    if [ -z "$NODEID" ]; then
        NODEID="$1"
    else
        PYTEST_ARGS+=("$1")
    fi
    shift
done

if [ -z "$NODEID" ]; then
    NODEID="$(first_core_auth_nodeid)"
    if [ -z "$NODEID" ]; then
        log_error "Failed to determine default nodeid from core/test_auth.py"
        exit 1
    fi
fi

# ─── Main ────────────────────────────────────────────────────────────
if bay_is_running; then
    log_info "Bay is already running on port ${BAY_PORT} — reusing existing environment"
    SETUP_BY_US=false
else
    log_info "Bay is NOT running on port ${BAY_PORT} — setting up full environment"
    SETUP_BY_US=true

    detect_backend

    # Prerequisites
    log_info "Checking prerequisites..."
    if ! docker info >/dev/null 2>&1; then log_error "Docker is not running"; exit 1; fi
    log_info "✓ Docker is available"
    if ! command -v kubectl >/dev/null 2>&1; then log_error "kubectl is not installed"; exit 1; fi
    log_info "✓ kubectl is available"
    if command -v lsof >/dev/null 2>&1 && lsof -i :$BAY_PORT >/dev/null 2>&1; then
        log_error "Port $BAY_PORT is already in use (but Bay is not healthy)"
        exit 1
    fi
    log_info "✓ Port $BAY_PORT is available"

    # Build images
    build_images

    # Setup cluster (kind create / docker-desktop verify / existing verify + kind load)
    case "$BACKEND" in
        kind)
            log_info "Setting up kind cluster: $KIND_CLUSTER_NAME..."
            if kind get clusters 2>/dev/null | grep -q "^${KIND_CLUSTER_NAME}$"; then
                log_warn "Cluster $KIND_CLUSTER_NAME already exists, deleting..."
                kind delete cluster --name "$KIND_CLUSTER_NAME"
            fi
            kind create cluster --name "$KIND_CLUSTER_NAME" --wait 60s
            log_info "✓ kind cluster created"
            load_images_to_kind "$KIND_CLUSTER_NAME"
            ;;
        docker-desktop|existing)
            if ! kubectl cluster-info >/dev/null 2>&1; then
                log_error "Cannot connect to Kubernetes cluster"
                exit 1
            fi
            log_info "✓ Kubernetes cluster is accessible"
            KIND_CLUSTER=$(detect_kind_cluster_name || echo "")
            if [ -n "$KIND_CLUSTER" ]; then
                log_info "Detected kind cluster: $KIND_CLUSTER"
                load_images_to_kind "$KIND_CLUSTER"
            else
                log_warn "Note: Using imagePullPolicy=Never. Ensure images are available in the cluster."
            fi
            ;;
    esac

    setup_namespace_and_rbac
    deploy_bay_pod
    start_port_forward
fi

# Run the test
export E2E_BAY_PORT="$BAY_PORT"
export E2E_DRIVER_TYPE="k8s"
export E2E_K8S_NAMESPACE="$NAMESPACE"

log_info "Running: ${NODEID}"
uv run pytest "$NODEID" "${PYTEST_ARGS[@]}"
