#!/bin/bash
# Run Bay E2E tests with Kubernetes driver
#
# In this mode:
# - Bay runs as a Pod in K8s cluster (uses in-cluster config)
# - Ship Pods are created in the same namespace
# - Bay connects to Ship via Pod IP
#
# Supports multiple K8s backends:
# - kind: Create a kind cluster (default if kind is installed)
# - docker-desktop: Use Docker Desktop's built-in Kubernetes
# - existing: Use current kubectl context (any existing cluster)
#
# Prerequisites:
# - Docker daemon running
# - kubectl installed
# - For kind mode: kind installed
# - For docker-desktop: Docker Desktop with Kubernetes enabled
# - ship:latest and bay:latest images built
#
# Usage:
#   ./run.sh                    # Auto-detect backend
#   ./run.sh --backend kind     # Force use kind
#   ./run.sh --backend docker-desktop  # Force use Docker Desktop K8s
#   ./run.sh -v                 # Verbose mode
#   ./run.sh -k "test_create"   # Run specific test

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BAY_DIR="$(cd "${SCRIPT_DIR}/../../.." && pwd)"
NAMESPACE="bay-e2e-test"
BAY_PORT=8003  # Port forwarded to localhost
KIND_CLUSTER_NAME="bay-e2e-test"
BACKEND=""  # Will be detected or set via --backend
PORT_FORWARD_PID=""

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
    
    # Stop port-forward if running
    if [ -n "$PORT_FORWARD_PID" ] && kill -0 "$PORT_FORWARD_PID" 2>/dev/null; then
        log_info "Stopping port-forward..."
        kill "$PORT_FORWARD_PID" 2>/dev/null || true
        wait "$PORT_FORWARD_PID" 2>/dev/null || true
    fi
    
    # Delete namespace (this cleans up all Pods, PVCs, etc.)
    if kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
        log_info "Deleting namespace $NAMESPACE..."
        kubectl delete namespace "$NAMESPACE" --grace-period=0 --force 2>/dev/null || true
        # Wait for namespace to be deleted (with timeout)
        local max_wait=60
        local waited=0
        while kubectl get namespace "$NAMESPACE" >/dev/null 2>&1 && [ $waited -lt $max_wait ]; do
            sleep 1
            waited=$((waited + 1))
        done
        if [ $waited -ge $max_wait ]; then
            log_warn "Namespace deletion timed out, may need manual cleanup"
        fi
    fi
    
    # If using kind, delete the cluster
    if [ "$BACKEND" = "kind" ]; then
        log_info "Deleting kind cluster..."
        kind delete cluster --name "$KIND_CLUSTER_NAME" 2>/dev/null || true
    fi
    
    log_info "Cleanup complete"
}

detect_backend() {
    if [ -n "$BACKEND" ]; then
        log_info "Using specified backend: $BACKEND"
        return
    fi
    
    # Auto-detect: check current kubectl context first
    CURRENT_CONTEXT=$(kubectl config current-context 2>/dev/null || echo "")
    
    if [ -z "$CURRENT_CONTEXT" ]; then
        # No kubectl context, need to create a cluster
        if command -v kind >/dev/null 2>&1; then
            BACKEND="kind"
            log_info "No kubectl context found, will use kind"
        else
            log_error "No kubectl context and kind not installed"
            log_error "Install kind: https://kind.sigs.k8s.io/"
            log_error "Or enable Docker Desktop Kubernetes"
            exit 1
        fi
        return
    fi
    
    # Check if it's docker-desktop
    if [[ "$CURRENT_CONTEXT" == "docker-desktop" ]] || [[ "$CURRENT_CONTEXT" == "docker-for-desktop" ]]; then
        BACKEND="docker-desktop"
        log_info "Detected Docker Desktop Kubernetes"
        return
    fi
    
    # Check if cluster is accessible
    if kubectl cluster-info >/dev/null 2>&1; then
        BACKEND="existing"
        log_info "Using existing cluster (context: $CURRENT_CONTEXT)"
        return
    fi
    
    # Can't connect, try kind
    if command -v kind >/dev/null 2>&1; then
        BACKEND="kind"
        log_info "Cannot connect to cluster, will use kind"
    else
        log_error "Cannot connect to cluster and kind not installed"
        exit 1
    fi
}

check_prerequisites() {
    log_info "Checking prerequisites..."
    
    # Check Docker
    if ! docker info >/dev/null 2>&1; then
        log_error "Docker is not running or not accessible"
        exit 1
    fi
    log_info "✓ Docker is available"
    
    # Check kubectl
    if ! command -v kubectl >/dev/null 2>&1; then
        log_error "kubectl is not installed"
        exit 1
    fi
    log_info "✓ kubectl is available"
    
    # Check kind if needed
    if [ "$BACKEND" = "kind" ]; then
        if ! command -v kind >/dev/null 2>&1; then
            log_error "kind is not installed. Install from: https://kind.sigs.k8s.io/"
            exit 1
        fi
        log_info "✓ kind is available"
    fi
    
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

setup_kind_cluster() {
    log_info "Setting up kind cluster: $KIND_CLUSTER_NAME..."
    
    # Check if cluster already exists
    if kind get clusters 2>/dev/null | grep -q "^${KIND_CLUSTER_NAME}$"; then
        log_warn "Cluster $KIND_CLUSTER_NAME already exists, deleting..."
        kind delete cluster --name "$KIND_CLUSTER_NAME"
    fi
    
    # Create kind cluster
    kind create cluster --name "$KIND_CLUSTER_NAME" --wait 60s
    
    log_info "✓ kind cluster created"
    
    # Load images into kind
    load_images_to_kind "$KIND_CLUSTER_NAME"
}

# Detect if the current cluster is a kind cluster and return its name
detect_kind_cluster_name() {
    # Check if any node has the kind label or naming pattern
    local node_names
    node_names=$(kubectl get nodes -o jsonpath='{.items[*].metadata.name}' 2>/dev/null || echo "")
    
    # kind nodes are named like: <cluster-name>-control-plane, <cluster-name>-worker, etc.
    for node in $node_names; do
        if [[ "$node" == *"-control-plane" ]]; then
            # Extract cluster name from node name
            local cluster_name="${node%-control-plane}"
            # Verify it's actually a kind cluster by checking kind clusters
            if command -v kind >/dev/null 2>&1; then
                if kind get clusters 2>/dev/null | grep -q "^${cluster_name}$"; then
                    echo "$cluster_name"
                    return 0
                fi
            fi
            # Even if kind command not found, assume it's a kind cluster based on naming
            echo "$cluster_name"
            return 0
        fi
    done
    
    # Not a kind cluster
    return 1
}

load_images_to_kind() {
    local cluster_name="$1"
    log_info "Loading images into kind cluster '$cluster_name'..."
    kind load docker-image ship:latest --name "$cluster_name"
    kind load docker-image bay:latest --name "$cluster_name"
    log_info "✓ Images loaded into kind cluster"
}

setup_namespace_and_rbac() {
    log_info "Setting up namespace and RBAC..."
    
    # Delete existing namespace if exists (clean slate)
    if kubectl get namespace "$NAMESPACE" >/dev/null 2>&1; then
        log_warn "Namespace $NAMESPACE exists, deleting for clean slate..."
        kubectl delete namespace "$NAMESPACE" --grace-period=0 --force 2>/dev/null || true
        # Wait for deletion
        sleep 3
    fi
    
    # Create namespace
    kubectl create namespace "$NAMESPACE"
    log_info "✓ Created namespace $NAMESPACE"
    
    # Create ServiceAccount for Bay
    kubectl apply -f - <<EOF
apiVersion: v1
kind: ServiceAccount
metadata:
  name: bay
  namespace: $NAMESPACE
EOF
    
    # Create Role with required permissions
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
    
    # Create RoleBinding
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

deploy_bay_pod() {
    log_info "Deploying Bay Pod..."
    
    # Create ConfigMap with Bay config
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
      port: 8000

    database:
      url: "sqlite+aiosqlite:///./bay-e2e-test.db"
      echo: false

    driver:
      type: k8s
      k8s:
        namespace: "$NAMESPACE"
        kubeconfig: null  # Use in-cluster config
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
    
    # Create Bay Pod
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
      imagePullPolicy: Never  # Use local image
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
          port: 8000
        initialDelaySeconds: 5
        periodSeconds: 2
      livenessProbe:
        httpGet:
          path: /health
          port: 8000
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
    
    # Show pod status while waiting for better debugging
    local max_wait=120
    local waited=0
    local interval=5
    
    while [ $waited -lt $max_wait ]; do
        POD_STATUS=$(kubectl get pod/bay -n "$NAMESPACE" -o jsonpath='{.status.phase}' 2>/dev/null || echo "Unknown")
        CONTAINER_STATUS=$(kubectl get pod/bay -n "$NAMESPACE" -o jsonpath='{.status.containerStatuses[0].state}' 2>/dev/null | grep -oE '"[^"]+":' | head -1 | tr -d '":' || echo "unknown")
        
        if [ "$POD_STATUS" = "Running" ]; then
            # Check if ready
            READY=$(kubectl get pod/bay -n "$NAMESPACE" -o jsonpath='{.status.containerStatuses[0].ready}' 2>/dev/null || echo "false")
            if [ "$READY" = "true" ]; then
                log_info "✓ Bay Pod is ready"
                return 0
            fi
        fi
        
        # Show status every interval
        log_info "  Pod status: $POD_STATUS, Container: $CONTAINER_STATUS (waited ${waited}s / ${max_wait}s)"
        
        # If pod is stuck in ImagePullBackOff or ErrImagePull, show details and fail fast
        if [[ "$CONTAINER_STATUS" == *"ImagePull"* ]] || [[ "$CONTAINER_STATUS" == *"ErrImage"* ]]; then
            log_error "Pod is stuck trying to pull image"
            kubectl describe pod/bay -n "$NAMESPACE" | tail -20
            exit 1
        fi
        
        # If pod failed, show logs
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
    log_error "Pod status:"
    kubectl describe pod/bay -n "$NAMESPACE" | tail -30
    exit 1
}

start_port_forward() {
    log_info "Starting port-forward to Bay Pod..."
    
    kubectl port-forward pod/bay -n "$NAMESPACE" $BAY_PORT:8000 &
    PORT_FORWARD_PID=$!
    
    # Wait for port-forward to be ready
    sleep 2
    
    # Verify port-forward is working
    local max_attempts=30
    local attempt=1
    
    while [ $attempt -le $max_attempts ]; do
        if curl -s "http://127.0.0.1:$BAY_PORT/health" >/dev/null 2>&1; then
            log_info "✓ Port-forward is ready"
            return 0
        fi
        
        # Check if port-forward process is still running
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

run_tests() {
    local parallel_mode="$1"
    local num_workers="$2"
    shift 2

    log_info "Running E2E tests (k8s mode, backend: $BACKEND)..."
    
    cd "$BAY_DIR"
    
    # Set environment variables for tests
    export E2E_BAY_PORT=$BAY_PORT
    export E2E_DRIVER_TYPE=k8s
    export E2E_K8S_NAMESPACE=$NAMESPACE

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
        uv run pytest tests/integration -v "$@"
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
        --backend)
            BACKEND="$2"
            shift 2
            ;;
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

# Validate backend if specified
if [ -n "$BACKEND" ]; then
    case "$BACKEND" in
        kind|docker-desktop|existing)
            ;;
        *)
            log_error "Invalid backend: $BACKEND"
            log_error "Valid options: kind, docker-desktop, existing"
            exit 1
            ;;
    esac
fi

# Main execution
detect_backend
check_prerequisites
build_images

# Setup cluster based on backend
case "$BACKEND" in
    kind)
        setup_kind_cluster
        ;;
    docker-desktop|existing)
        # Verify cluster is accessible
        if ! kubectl cluster-info >/dev/null 2>&1; then
            log_error "Cannot connect to Kubernetes cluster"
            if [ "$BACKEND" = "docker-desktop" ]; then
                log_error "Enable Kubernetes in Docker Desktop: Settings -> Kubernetes -> Enable Kubernetes"
            fi
            exit 1
        fi
        log_info "✓ Kubernetes cluster is accessible"
        
        # Check if the cluster is actually a kind cluster (common misconfiguration)
        # kind clusters need images to be loaded explicitly
        KIND_CLUSTER=$(detect_kind_cluster_name || echo "")
        if [ -n "$KIND_CLUSTER" ]; then
            log_info "Detected kind cluster: $KIND_CLUSTER"
            load_images_to_kind "$KIND_CLUSTER"
        else
            log_warn "Note: Using imagePullPolicy=Never. Ensure images are available in the cluster."
            log_warn "For kind clusters, images must be loaded with: kind load docker-image <image> --name <cluster>"
        fi
        ;;
esac

setup_namespace_and_rbac
deploy_bay_pod
start_port_forward
run_tests "$PARALLEL_MODE" "$NUM_WORKERS" $PYTEST_ARGS

log_info "E2E tests completed successfully!"
