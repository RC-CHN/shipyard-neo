# K8s E2E Tests

This directory contains scripts to run Bay E2E tests with the Kubernetes driver.

## Architecture

**Bay runs as a Pod inside the K8s cluster**, using in-cluster configuration to communicate with the Kubernetes API.

```
┌─────────────────────────────────────────────────────────┐
│  Host (tests run here)                                  │
│                                                         │
│  ┌─────────────────┐                                   │
│  │  pytest         │ ──port-forward──→ Bay Pod        │
│  │  (E2E tests)    │                                   │
│  └─────────────────┘                                   │
│                                                         │
│  ┌─────────────────────────────────────────────────────┤
│  │  K8s cluster (kind / docker-desktop / existing)     │
│  │                                                      │
│  │  Namespace: bay-e2e-test                            │
│  │  ┌──────────┐                                       │
│  │  │ Bay Pod  │ ───in-cluster-config───→ K8s API    │
│  │  │ (bay:    │                                       │
│  │  │  latest) │ ───Pod IP───→ Ship Pods              │
│  │  └──────────┘                                       │
│  │                                                      │
│  │  ┌──────────┐  ┌──────────┐  ┌──────────┐          │
│  │  │ Ship Pod │  │ Ship Pod │  │   PVC    │          │
│  │  │ (session)│  │ (session)│  │ (cargo)  │          │
│  │  └──────────┘  └──────────┘  └──────────┘          │
│  │                                                      │
│  └─────────────────────────────────────────────────────┤
└─────────────────────────────────────────────────────────┘
```

## Supported Backends

| Backend | Description | Auto-detected |
|---------|-------------|---------------|
| `docker-desktop` | Docker Desktop's built-in Kubernetes | Yes (if context is docker-desktop) |
| `kind` | [kind](https://kind.sigs.k8s.io/) - Kubernetes in Docker | Yes (if kubectl has no context) |
| `existing` | Any existing Kubernetes cluster | Yes (if kubectl has accessible context) |

## Prerequisites

- Docker daemon running
- [kubectl](https://kubernetes.io/docs/tasks/tools/) installed
- For kind backend: [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation) installed
- For docker-desktop backend: Docker Desktop with Kubernetes enabled

## Usage

```bash
# Auto-detect backend (recommended)
./run.sh

# Force specific backend
./run.sh --backend docker-desktop  # Use Docker Desktop K8s
./run.sh --backend kind             # Use kind cluster
./run.sh --backend existing         # Use current kubectl context

# Test options
./run.sh -v                         # Verbose mode
./run.sh -k "test_create"           # Run specific test
./run.sh -m "not serial"            # Run with markers
```

## What it does

1. **Detect backend**: Auto-detect or use specified K8s backend
2. **Check prerequisites**: Verifies Docker, kubectl, and backend-specific tools
3. **Build images**: Builds `ship:latest` and `bay:latest` Docker images
4. **Setup cluster** (kind only): Creates a local K8s cluster and loads images
5. **Setup RBAC**: Creates namespace, ServiceAccount, Role, and RoleBinding
6. **Deploy Bay Pod**: Deploys Bay as a Pod with ConfigMap configuration
7. **Port-forward**: Exposes Bay to localhost via kubectl port-forward
8. **Run tests**: Executes pytest integration tests
9. **Cleanup**: Deletes namespace and cluster (kind only) on exit

## RBAC Permissions

The test script creates the following RBAC resources in the `bay-e2e-test` namespace:

- **ServiceAccount**: `bay`
- **Role**: `bay-role` with permissions:
  - `pods`: create, delete, get, list, watch
  - `pods/log`: get
  - `persistentvolumeclaims`: create, delete, get, list, watch
- **RoleBinding**: `bay-rolebinding` binding the role to the service account

## Cleanup

The script uses a trap to ensure cleanup on exit (Ctrl+C or test completion):

1. Stops kubectl port-forward
2. Deletes the `bay-e2e-test` namespace (cleans up all Pods, PVCs)
3. Deletes the kind cluster (kind backend only)

## Docker Desktop Kubernetes Setup

If using Docker Desktop:

1. Open Docker Desktop
2. Go to Settings → Kubernetes
3. Check "Enable Kubernetes"
4. Click "Apply & Restart"
5. Wait for Kubernetes to be ready (green indicator)

Then run:
```bash
./run.sh --backend docker-desktop
```

## Troubleshooting

### Bay Pod not starting
Check Bay Pod logs:
```bash
kubectl logs pod/bay -n bay-e2e-test
kubectl describe pod/bay -n bay-e2e-test
```

### Ship Pod stuck in Pending
Check if there's enough resources or if the image is available:
```bash
kubectl describe pods -n bay-e2e-test -l bay.managed=true
kubectl get events -n bay-e2e-test
```

### PVC stuck in Pending
Check if a default StorageClass exists:
```bash
kubectl get storageclass
```

For Docker Desktop, `hostpath` is the default. For kind, the built-in storage class should work.
