# Bay Test Scripts

This directory contains test scripts and configurations for different deployment modes.

## Directory Structure

```
scripts/
├── docker-host/          # Bay on host, Ship in container (host_port mode)
│   ├── config.yaml       # Configuration for this mode
│   └── run.sh            # Script to run E2E tests
├── docker-network/       # Bay in container, Ship in container (container_network mode)
│   ├── config.yaml       # Configuration for this mode
│   ├── docker-compose.yaml  # Compose file to run Bay
│   └── run.sh            # Script to run E2E tests
└── k8s/                  # (Future) Bay with Kubernetes driver
```

## Docker Host Mode

The most common development setup where:
- Bay runs on the host machine
- Ship containers are created via Docker
- Bay connects to Ship via host port mapping (`127.0.0.1:<random_port>`)

### Prerequisites

1. Docker daemon running
2. `ship:latest` image built:
   ```bash
   cd pkgs/ship && make build
   ```

### Running Tests

```bash
# From pkgs/bay directory
./tests/scripts/docker-host/run.sh

# With options
./tests/scripts/docker-host/run.sh -v                    # Verbose
./tests/scripts/docker-host/run.sh -k "test_create"      # Specific test
./tests/scripts/docker-host/run.sh --tb=long             # Long traceback
```

### Manual Testing

You can also start Bay manually and run tests:

```bash
# Terminal 1: Start Bay with test config
cd pkgs/bay
BAY_CONFIG_FILE=tests/scripts/docker-host/config.yaml uv run python -m app.main

# Terminal 2: Run tests
cd pkgs/bay
uv run pytest tests/integration -v
```

## Docker Network Mode

Production-like setup where:
- Bay runs in a Docker container
- Ship containers are created and connected to the same Docker network
- Bay connects to Ship via container network IP (direct connection)

### Prerequisites

1. Docker daemon running
2. `ship:latest` image built:
   ```bash
   cd pkgs/ship && make build
   ```
3. `bay:latest` image built (or the script will build it):
   ```bash
   cd pkgs/bay && make build
   ```

### Running Tests

```bash
# From pkgs/bay directory
./tests/scripts/docker-network/run.sh

# With options
./tests/scripts/docker-network/run.sh -v                    # Verbose
./tests/scripts/docker-network/run.sh -k "test_create"      # Specific test
```

### Manual Testing

```bash
# Start Bay in container
cd pkgs/bay/tests/scripts/docker-network
docker-compose up -d

# Check logs
docker-compose logs -f

# Run tests (from pkgs/bay directory)
cd pkgs/bay
E2E_BAY_PORT=8002 uv run pytest tests/integration -v

# Stop
docker-compose down
```

## Mode Comparison

| Mode | Bay Location | Ship Location | Connection | Use Case |
|:--|:--|:--|:--|:--|
| docker-host | Host machine | Container | host_port mapping | Development |
| docker-network | Container | Container | container IP | Testing / Staging |
| k8s (future) | K8s Pod | K8s Pod | K8s Service | Production |

## Adding New Driver Modes

To add a new driver mode (e.g., kubernetes):

1. Create a new directory: `scripts/k8s/`
2. Add configuration: `scripts/k8s/config.yaml`
3. Add run script: `scripts/k8s/run.sh`
4. The same `tests/integration/test_e2e_api.py` should work for all modes

The E2E tests in `tests/integration/` are designed to be driver-agnostic -
they only interact with Bay's HTTP API and should work regardless of the
underlying driver (docker, k8s, etc.).
