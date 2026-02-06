# Shipyard Neo Python SDK

[![PyPI version](https://badge.fury.io/py/shipyard-neo-sdk.svg)](https://badge.fury.io/py/shipyard-neo-sdk)
[![Python 3.10+](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/downloads/)
[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

Python SDK for [Shipyard Neo](https://github.com/your-org/shipyard-neo) (Bay API) - A secure sandbox execution environment for AI agents and code execution.

## Installation

```bash
pip install shipyard-neo-sdk
```

## Quick Start

```python
import asyncio
from shipyard_neo import BayClient

async def main():
    async with BayClient(
        endpoint_url="http://localhost:8000",
        access_token="your-token"
    ) as client:
        # Create a sandbox
        sandbox = await client.create_sandbox(
            profile="python-default",
            ttl=3600,  # 1 hour
        )
        
        # Execute Python code
        result = await sandbox.python.exec("print('Hello, World!')")
        print(result.output)
        
        # File operations
        await sandbox.filesystem.write_file("hello.txt", "Hello!")
        content = await sandbox.filesystem.read_file("hello.txt")
        
        # Shell commands
        result = await sandbox.shell.exec("ls -la")
        print(result.output)
        
        # Cleanup
        await sandbox.delete()

asyncio.run(main())
```

## Features

- **Async-first**: Built with `async/await` for high concurrency
- **Type-safe**: Full type hints with Pydantic models
- **Error handling**: SDK exceptions map 1:1 with Bay API error codes
- **Idempotency**: Safe retries with idempotency keys
- **Capabilities**: Python execution, Shell commands, Filesystem operations


## License

AGPL-3.0-or-later
