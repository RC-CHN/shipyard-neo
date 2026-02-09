# Shipyard Neo Python SDK

Python async SDK for Bay API.  
Use it to create sandboxes, run code, manage persistent cargo, and build skills self-update workflows.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

## Features

- Async-first client (`httpx`)
- Typed models (`pydantic`)
- Python / Shell / Filesystem capabilities
- Execution history query + annotation
- Skill lifecycle APIs (candidate/evaluate/promote/release/rollback)
- External cargo management
- Idempotency support for critical operations

## Installation

```bash
pip install shipyard-neo-sdk
```

Or from source:

```bash
cd shipyard-neo-sdk
pip install -e .
```

## Quick Start

```python
import asyncio
from shipyard_neo import BayClient


async def main():
    async with BayClient(
        endpoint_url="http://localhost:8000",
        access_token="your-token",
    ) as client:
        sandbox = await client.create_sandbox(profile="python-default", ttl=600)

        py = await sandbox.python.exec(
            "print('hello')",
            include_code=True,
            description="smoke run",
            tags="smoke,python",
        )
        print(py.output)
        print(py.execution_id, py.execution_time_ms)

        history = await sandbox.get_execution_history(limit=10)
        print("history entries:", history.total)

        await sandbox.delete()


asyncio.run(main())
```

## Client API

### `BayClient(...)`

```python
from shipyard_neo import BayClient

async with BayClient(
    endpoint_url="http://localhost:8000",
    access_token="your-token",
    timeout=30.0,
    max_retries=3,
) as client:
    ...
```

### Methods / Properties

| API | Description |
|:--|:--|
| `create_sandbox(...)` | Create sandbox |
| `get_sandbox(sandbox_id)` | Get one sandbox |
| `list_sandboxes(...)` | List sandboxes |
| `cargos` | `CargoManager` |
| `skills` | `SkillManager` |

## Sandbox API

### Lifecycle

| Method | Description |
|:--|:--|
| `refresh()` | Refresh sandbox state |
| `stop()` | Stop current session, keep data |
| `delete()` | Delete sandbox and managed resources |
| `extend_ttl(seconds, idempotency_key=None)` | Extend TTL |
| `keepalive()` | Extend idle timeout only |

### Execution History

| Method | Description |
|:--|:--|
| `get_execution_history(...)` | Query history list |
| `get_execution(execution_id)` | Get one entry |
| `get_last_execution(exec_type=None)` | Get latest entry |
| `annotate_execution(...)` | Update `description/tags/notes` |

Example:

```python
history = await sandbox.get_execution_history(
    exec_type="python",
    success_only=True,
    tags="etl",
    has_notes=False,
    has_description=True,
    limit=20,
)

last = await sandbox.get_last_execution(exec_type="python")
await sandbox.annotate_execution(
    last.id,
    description="useful snippet",
    tags="etl,stable",
    notes="candidate source",
)
```

## Capabilities

### Python

```python
result = await sandbox.python.exec(
    "print('hello')",
    timeout=30,
    include_code=True,
    description="python exec",
    tags="demo,python",
)
```

`PythonExecResult` fields:

- `success`
- `output`
- `error`
- `data`
- `execution_id`
- `execution_time_ms`
- `code`

### Shell

```python
result = await sandbox.shell.exec(
    "echo hello",
    cwd=".",
    timeout=30,
    include_code=True,
    description="shell exec",
    tags="demo,shell",
)
```

`ShellExecResult` fields:

- `success`
- `output`
- `error`
- `exit_code`
- `execution_id`
- `execution_time_ms`
- `command`

### Filesystem

```python
await sandbox.filesystem.write_file("app.py", "print('hi')")
content = await sandbox.filesystem.read_file("app.py")
entries = await sandbox.filesystem.list_dir(".")
await sandbox.filesystem.delete("app.py")

await sandbox.filesystem.upload("bin/model.bin", b"binary-bytes")
blob = await sandbox.filesystem.download("bin/model.bin")
```

## Cargo API (`client.cargos`)

```python
cargo = await client.cargos.create(size_limit_mb=1024)

sandbox = await client.create_sandbox(
    profile="python-default",
    cargo_id=cargo.id,  # attach external cargo
    ttl=600,
)

await sandbox.filesystem.write_file("state.txt", "persist-me")
await sandbox.delete()  # external cargo still exists

sandbox2 = await client.create_sandbox(profile="python-default", cargo_id=cargo.id)
assert await sandbox2.filesystem.read_file("state.txt") == "persist-me"

await sandbox2.delete()
await client.cargos.delete(cargo.id)
```

## Skill Lifecycle API (`client.skills`)

Use this to build reusable skills from execution evidence.

```python
from shipyard_neo import SkillReleaseStage

# 1) collect execution evidence
e1 = await sandbox.python.exec("print('step1')", tags="etl")
e2 = await sandbox.shell.exec("echo step2", tags="etl")

# 2) create candidate
candidate = await client.skills.create_candidate(
    skill_key="etl-loader",
    source_execution_ids=[e1.execution_id, e2.execution_id],
    scenario_key="csv-import",
    payload_ref="s3://skills/etl-loader/v1",
)

# 3) evaluate
evaluation = await client.skills.evaluate_candidate(
    candidate.id,
    passed=True,
    score=0.96,
    benchmark_id="bench-etl-001",
    report="all checks passed",
)

# 4) promote
release = await client.skills.promote_candidate(
    candidate.id,
    stage=SkillReleaseStage.CANARY,
)

# 5) list / rollback
releases = await client.skills.list_releases(skill_key="etl-loader", active_only=True)
rollback_release = await client.skills.rollback_release(release.id)
```

## Idempotency

```python
sandbox = await client.create_sandbox(
    profile="python-default",
    ttl=600,
    idempotency_key="create-req-001",
)

await sandbox.extend_ttl(300, idempotency_key="extend-req-001")
cargo = await client.cargos.create(size_limit_mb=512, idempotency_key="cargo-req-001")
```

## Reliability / Retry Policy

`max_retries` is now enforced in the HTTP pipeline.

- Auto-retry methods: `GET`, `PUT`, `DELETE`
- `POST` retries only when `idempotency_key` is provided
- Retryable failures: transport timeout/connection errors, HTTP `429`, HTTP `5xx`
- Backoff: bounded exponential backoff

This keeps retries safe for non-idempotent operations while still protecting against transient faults.

## Error Handling

All exceptions inherit from `BayError`.

```python
from shipyard_neo import BayError, NotFoundError, ConflictError

try:
    sb = await client.get_sandbox("sandbox-missing")
except NotFoundError:
    ...
except ConflictError:
    ...
except BayError as e:
    print(e.message, e.details)
```

For non-JSON error responses (e.g. proxy HTML error pages), the SDK keeps status-based exception mapping and includes a bounded raw response snippet in `details` for diagnosis.

### Error Types

| Exception | HTTP Code | Meaning |
|:--|:--|:--|
| `UnauthorizedError` | 401 | Invalid/missing auth |
| `ForbiddenError` | 403 | Permission denied |
| `NotFoundError` | 404 | Resource not found |
| `QuotaExceededError` | 429 | Rate/quota limit |
| `ConflictError` | 409 | State conflict |
| `ValidationError` | 400 | Invalid request |
| `SessionNotReadyError` | 503 | Session not ready |
| `RequestTimeoutError` | 504 | Upstream timeout |
| `ShipError` | 502 | Runtime error from Ship |
| `SandboxExpiredError` | 409 | TTL already expired |
| `SandboxTTLInfiniteError` | 409 | Infinite TTL cannot be extended |
| `CapabilityNotSupportedError` | 400 | Capability not allowed |
| `InvalidPathError` | 400 | Invalid workspace path |
| `CargoFileNotFoundError` | 404 | File not found |

## Environment Variables

`BayClient` fallback env vars:

| Variable | Description |
|:--|:--|
| `BAY_ENDPOINT` | Bay API base URL |
| `BAY_TOKEN` | Bearer token |
| `BAY_TIMEOUT` | Default timeout (seconds) |
| `BAY_MAX_RETRIES` | Max retry attempts |

```python
import os
from shipyard_neo import BayClient

os.environ["BAY_ENDPOINT"] = "http://localhost:8000"
os.environ["BAY_TOKEN"] = "your-token"

# endpoint/token omitted -> use env vars
async with BayClient() as client:
    ...
```

## License

AGPL-3.0-or-later
