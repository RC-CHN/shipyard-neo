# Shipyard Neo Python SDK

A Python client library for the Bay API - secure sandbox execution for AI agents.

[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

## Features

- **Async-first design** - Built on `httpx` for modern async Python
- **Type-safe** - Full type hints with Pydantic models
- **Capability-based access** - Python, Shell, and Filesystem operations
- **Automatic session management** - Lazy startup, transparent resume
- **Idempotency support** - Safe retries for network failures
- **Persistent storage** - Cargo volumes survive sandbox restarts

## Installation

```bash
pip install shipyard-neo-sdk
```

Or install from source:

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
        # Create a sandbox
        sandbox = await client.create_sandbox(profile="python-default", ttl=600)
        
        # Execute Python code
        result = await sandbox.python.exec("print('Hello, World!')")
        print(result.output)  # "Hello, World!\n"
        
        # Execute shell commands
        result = await sandbox.shell.exec("ls -la")
        print(result.output)
        
        # File operations
        await sandbox.filesystem.write_file("app.py", "print('hi')")
        content = await sandbox.filesystem.read_file("app.py")
        
        # Cleanup
        await sandbox.delete()

asyncio.run(main())
```

## API Reference

### BayClient

The main entry point for the SDK.

```python
from shipyard_neo import BayClient

# Using environment variables (SHIPYARD_ENDPOINT_URL, SHIPYARD_ACCESS_TOKEN)
async with BayClient() as client:
    ...

# Explicit configuration
async with BayClient(
    endpoint_url="http://localhost:8000",
    access_token="your-token",
    timeout=30.0,  # Default request timeout
) as client:
    ...
```

#### Methods

| Method | Description |
|:--|:--|
| `create_sandbox()` | Create a new sandbox |
| `get_sandbox(id)` | Get an existing sandbox |
| `list_sandboxes()` | List all sandboxes |
| `cargos` | Access CargoManager for cargo operations |

### Sandbox Creation

```python
# Basic creation
sandbox = await client.create_sandbox(
    profile="python-default",  # Profile ID (default: "python-default")
    ttl=600,                   # Time-to-live in seconds (optional)
)

# With external cargo
cargo = await client.cargos.create(size_limit_mb=512)
sandbox = await client.create_sandbox(
    profile="python-default",
    cargo_id=cargo.id,  # Attach external cargo
    ttl=600,
)

# With idempotency key (for safe retries)
sandbox = await client.create_sandbox(
    profile="python-default",
    ttl=600,
    idempotency_key="unique-request-id-123",
)
```

### Sandbox Properties

```python
sandbox.id            # str: Unique sandbox ID
sandbox.status        # SandboxStatus: IDLE, STARTING, READY, FAILED, EXPIRED
sandbox.profile       # str: Profile ID
sandbox.cargo_id      # str: Associated cargo ID
sandbox.capabilities  # list[str]: ["python", "shell", "filesystem"]
sandbox.created_at    # datetime: Creation timestamp
sandbox.expires_at    # datetime | None: TTL expiration (None = infinite)
```

### Sandbox Lifecycle

```python
# Refresh local state from server
await sandbox.refresh()

# Stop the sandbox (reclaims compute, preserves files)
await sandbox.stop()

# Delete the sandbox permanently
await sandbox.delete()

# Extend TTL by N seconds
await sandbox.extend_ttl(300)  # Extend by 5 minutes

# With idempotency key
await sandbox.extend_ttl(300, idempotency_key="extend-001")

# Send keepalive (extends idle timeout only, NOT TTL)
await sandbox.keepalive()
```

### Listing Sandboxes

```python
from shipyard_neo import SandboxStatus

# List all sandboxes
result = await client.list_sandboxes()
for sb in result.items:
    print(f"{sb.id}: {sb.status}")

# With pagination
result = await client.list_sandboxes(limit=50)
while result.next_cursor:
    result = await client.list_sandboxes(cursor=result.next_cursor, limit=50)
    for sb in result.items:
        process(sb)

# Filter by status
result = await client.list_sandboxes(status=SandboxStatus.READY)
```

## Capabilities

### Python Capability

Execute Python code in an IPython kernel. Variables persist across calls.

```python
# Simple execution
result = await sandbox.python.exec("print('Hello!')")
assert result.success
print(result.output)  # "Hello!\n"

# Multi-line code
code = """
def fibonacci(n):
    if n <= 1: return n
    return fibonacci(n-1) + fibonacci(n-2)

result = fibonacci(10)
print(f"fib(10) = {result}")
"""
result = await sandbox.python.exec(code)

# Variable persistence
await sandbox.python.exec("x = 42")
result = await sandbox.python.exec("print(x)")  # Works!

# Error handling
result = await sandbox.python.exec("1 / 0")
if not result.success:
    print(result.error)  # "ZeroDivisionError: division by zero"

# Custom timeout
result = await sandbox.python.exec("import time; time.sleep(10)", timeout=15)
```

#### PythonExecResult

| Attribute | Type | Description |
|:--|:--|:--|
| `success` | `bool` | Whether execution completed without exception |
| `output` | `str` | stdout output |
| `error` | `str \| None` | Error traceback (on failure) |
| `data` | `dict \| None` | IPython rich output |

**Success Example:**

```python
result = await sandbox.python.exec("2 ** 10")
# result.success = True
# result.output = "1024\n"
# result.error = None
# result.data = {
#     'execution_count': 2,
#     'output': {'text': '1024', 'images': []}
# }
```

**Failure Example:**

```python
result = await sandbox.python.exec("1 / 0")
# result.success = False
# result.output = ""
# result.error = """
# ---------------------------------------------------------------------------
# ZeroDivisionError                         Traceback (most recent call last)
# Cell In[4], line 1
# ----> 1 1 / 0
#
# ZeroDivisionError: division by zero
# """
# result.data = None
```

### Shell Capability

Execute shell commands.

```python
# Simple command
result = await sandbox.shell.exec("echo 'Hello!'")
print(result.output)      # "Hello!\n"
print(result.exit_code)   # 0

# Pipe operations
result = await sandbox.shell.exec("ls -la | grep py")

# Custom working directory
result = await sandbox.shell.exec("pwd && ls", cwd="src")

# Exit code handling
result = await sandbox.shell.exec("exit 42")
assert not result.success
assert result.exit_code == 42

# Custom timeout
result = await sandbox.shell.exec("sleep 10", timeout=15)
```

#### ShellExecResult

| Attribute | Type | Description |
|:--|:--|:--|
| `success` | `bool` | Whether execution succeeded (exit_code == 0) |
| `output` | `str` | Combined stdout + stderr output |
| `error` | `str \| None` | Error message |
| `exit_code` | `int \| None` | Process exit code |

**Success Example:**

```python
result = await sandbox.shell.exec("whoami && pwd")
# result.success = True
# result.output = "shipyard\n/workspace\n"
# result.error = None
# result.exit_code = 0
```

**Failure Example:**

```python
result = await sandbox.shell.exec("exit 42")
# result.success = False
# result.output = ""
# result.error = None
# result.exit_code = 42
```

**Pipe Example:**

```python
result = await sandbox.shell.exec("echo -e 'apple\\nbanana\\ncherry' | grep an")
# result.success = True
# result.output = "banana\n"
# result.exit_code = 0
```

### Filesystem Capability

Read, write, and manage files in the sandbox workspace (`/workspace`).

```python
# Write text file
await sandbox.filesystem.write_file("app.py", "print('hello')")

# Write to nested path (directories created automatically)
await sandbox.filesystem.write_file("src/main.py", "# main code")

# Read text file
content = await sandbox.filesystem.read_file("app.py")

# List directory
entries = await sandbox.filesystem.list_dir(".")
for entry in entries:
    print(f"{entry.name}: {'dir' if entry.is_dir else 'file'}")

# List nested directory
entries = await sandbox.filesystem.list_dir("src")

# Delete file or directory
await sandbox.filesystem.delete("app.py")

# Upload binary file
binary_data = open("image.png", "rb").read()
await sandbox.filesystem.upload("assets/image.png", binary_data)

# Download binary file
data = await sandbox.filesystem.download("assets/image.png")
open("downloaded.png", "wb").write(data)
```

#### FileInfo

| Attribute | Type | Description |
|:--|:--|:--|
| `name` | `str` | File/directory name |
| `path` | `str` | Full path relative to /workspace |
| `is_dir` | `bool` | Whether it's a directory |
| `size` | `int \| None` | Size in bytes (None for directories) |
| `modified_at` | `datetime \| None` | Last modification time |

**Directory Listing Example:**

```python
entries = await sandbox.filesystem.list_dir(".")
# Returns: [FileInfo, FileInfo, ...]
#
# Directory example:
#   entry.name = "mydir"
#   entry.path = "mydir"
#   entry.is_dir = True
#   entry.size = None
#
# File example:
#   entry.name = "test.txt"
#   entry.path = "test.txt"
#   entry.is_dir = False
#   entry.size = 13

# Print all entries
for e in entries:
    if e.is_dir:
        print(f"📁 {e.name}/")
    else:
        print(f"📄 {e.name} ({e.size} bytes)")

# Typical output:
# 📁 mydir/
# 📁 nested/
# 📄 test.txt (13 bytes)
```

## Cargo Management

Cargo is persistent storage that survives sandbox restarts. There are two types:
- **Managed cargo**: Created automatically with sandbox, deleted with sandbox
- **External cargo**: Created separately, persists after sandbox deletion

```python
# Create external cargo
cargo = await client.cargos.create(size_limit_mb=512)
print(f"Created cargo: {cargo.id}")

# With idempotency key (for safe retries)
cargo = await client.cargos.create(
    size_limit_mb=512,
    idempotency_key="cargo-unique-123",
)

# Get cargo info
cargo = await client.cargos.get(cargo.id)
print(f"Managed: {cargo.managed}")
print(f"Size limit: {cargo.size_limit_mb} MB")

# List external cargos (managed cargos not included by default)
result = await client.cargos.list()
for c in result.items:
    print(f"{c.id}: {c.size_limit_mb} MB")

# Delete cargo
await client.cargos.delete(cargo.id)
```

### Using External Cargo

```python
# Create external cargo
cargo = await client.cargos.create(size_limit_mb=1024)

# Create sandbox with external cargo
sandbox = await client.create_sandbox(
    profile="python-default",
    cargo_id=cargo.id,
    ttl=600,
)

# Write data to cargo
await sandbox.filesystem.write_file("data.txt", "Important data")

# Delete sandbox (cargo persists!)
await sandbox.delete()

# Create new sandbox with same cargo
sandbox2 = await client.create_sandbox(
    profile="python-default",
    cargo_id=cargo.id,
    ttl=600,
)

# Data is still there!
content = await sandbox2.filesystem.read_file("data.txt")
assert content == "Important data"

# Cleanup
await sandbox2.delete()
await client.cargos.delete(cargo.id)
```

## Error Handling

All errors inherit from `BayError`.

```python
from shipyard_neo import (
    BayError,
    NotFoundError,
    UnauthorizedError,
    ForbiddenError,
    QuotaExceededError,
    ConflictError,
    ValidationError,
    SessionNotReadyError,
    RequestTimeoutError,
    ShipError,
    SandboxExpiredError,
    SandboxTTLInfiniteError,
    CapabilityNotSupportedError,
    InvalidPathError,
    CargoFileNotFoundError,
)

try:
    sandbox = await client.get_sandbox("nonexistent-id")
except NotFoundError as e:
    print(f"Sandbox not found: {e.message}")
except BayError as e:
    print(f"API error: {e.message}")
```

### Error Types

| Exception | HTTP Code | Description |
|:--|:--|:--|
| `UnauthorizedError` | 401 | Invalid or missing access token |
| `ForbiddenError` | 403 | Permission denied |
| `NotFoundError` | 404 | Resource not found |
| `QuotaExceededError` | 429 | Rate limit or quota exceeded |
| `ConflictError` | 409 | Resource conflict (e.g., cargo in use) |
| `ValidationError` | 422 | Invalid request parameters |
| `SessionNotReadyError` | 503 | Session not ready (try again) |
| `RequestTimeoutError` | 504 | Request timeout |
| `ShipError` | 502 | Ship (container) error |
| `SandboxExpiredError` | 410 | Sandbox TTL expired |
| `SandboxTTLInfiniteError` | 400 | Cannot extend infinite TTL |
| `CapabilityNotSupportedError` | 403 | Profile doesn't support capability |
| `InvalidPathError` | 400 | Invalid file path |
| `CargoFileNotFoundError` | 404 | File not found in workspace |

## Idempotency

For safe retries during network failures, use idempotency keys:

```python
# Sandbox creation
sandbox = await client.create_sandbox(
    profile="python-default",
    ttl=600,
    idempotency_key="unique-request-123",
)

# Retry with same key returns same sandbox
sandbox2 = await client.create_sandbox(
    profile="python-default",
    ttl=600,
    idempotency_key="unique-request-123",
)
assert sandbox.id == sandbox2.id

# TTL extension
await sandbox.extend_ttl(300, idempotency_key="extend-001")
# Retry returns same result
await sandbox.extend_ttl(300, idempotency_key="extend-001")
```

## Environment Variables

The SDK supports configuration via environment variables:

| Variable | Description |
|:--|:--|
| `SHIPYARD_ENDPOINT_URL` | Bay API endpoint URL |
| `SHIPYARD_ACCESS_TOKEN` | Authentication token |

```python
import os

os.environ["SHIPYARD_ENDPOINT_URL"] = "http://localhost:8000"
os.environ["SHIPYARD_ACCESS_TOKEN"] = "your-token"

# No explicit configuration needed
async with BayClient() as client:
    sandbox = await client.create_sandbox()
```

## Advanced Usage

### Session Lifecycle

Sessions are managed transparently. A capability call (Python/Shell/Filesystem) will automatically start a session if needed:

```python
# Sandbox starts in IDLE state (no session)
sandbox = await client.create_sandbox()
print(sandbox.status)  # SandboxStatus.IDLE

# First capability call triggers session start
result = await sandbox.python.exec("print('hello')")
await sandbox.refresh()
print(sandbox.status)  # SandboxStatus.READY

# Stop reclaims resources but preserves files
await sandbox.stop()
print(sandbox.status)  # SandboxStatus.IDLE

# Next capability call auto-resumes
result = await sandbox.python.exec("print('back!')")
# Note: Python variables are lost after stop
```

### Long-Running Tasks

For long-running operations, extend the timeout:

```python
# Python execution with longer timeout
result = await sandbox.python.exec(
    "import time; time.sleep(60)",
    timeout=120,
)

# Shell execution with longer timeout
result = await sandbox.shell.exec(
    "find / -name '*.py' 2>/dev/null",
    timeout=120,
)
```

### Keepalive for Idle Timeout

If you're between operations but want to prevent idle timeout:

```python
# Keepalive extends idle timeout but NOT TTL
await sandbox.keepalive()

# Note: keepalive does NOT start a session if none exists
```

## License

AGPL-3.0-or-later. See [LICENSE](./LICENSE) for details.
