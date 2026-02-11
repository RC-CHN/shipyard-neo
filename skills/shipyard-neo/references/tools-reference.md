# MCP Tools Reference

Complete parameter reference for all Shipyard Neo MCP tools. All tools are prefixed with `mcp--shipyard___neo--`.

## Sandbox Management

### `create_sandbox`

Create a new sandbox environment. Returns `sandbox_id` for subsequent operations.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `profile` | string | No | `python-default` | Runtime profile (e.g., `python-default`, `browser-python`). Use `list_profiles` to discover available options. |
| `ttl` | integer | No | 3600 | Time-to-live in seconds. Use `0` for no expiration. |

**Returns**: Sandbox ID, profile, status, capabilities list, TTL. When the sandbox has an active session, also returns `containers` list with each container's name, runtime_type, version, status, capabilities, and health.

### `delete_sandbox`

Delete a sandbox and clean up all resources (containers + Cargo volume).

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `sandbox_id` | string | Yes | The sandbox ID to delete |

### `list_profiles`

List available sandbox profiles. Profiles define runtime capabilities, resource limits, and idle timeout. Call this before `create_sandbox` to discover available profiles.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| *(none)* | — | — | This tool takes no parameters |

**Returns**: Profile ID, capabilities (python, shell, filesystem, browser), idle_timeout, and (when available) per-container topology.

**Common profiles**:

| Profile | Containers | Capabilities |
|---------|-----------|-------------|
| `python-default` | Ship only | python, shell, filesystem |
| `browser-python` | Ship + Gull | python, shell, filesystem, browser |

---

## Code Execution

### `execute_python`

Execute Python code in a sandbox. Variables persist across calls within the same sandbox session (IPython kernel).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `sandbox_id` | string | Yes | — | Target sandbox |
| `code` | string | Yes | — | Python code to execute |
| `timeout` | integer | No | 30 | Execution timeout in seconds (1-300) |
| `include_code` | boolean | No | false | Include executed code and execution metadata in response |
| `description` | string | No | — | Description for execution history annotation |
| `tags` | string | No | — | Comma-separated tags for execution history |

**Returns**: Success/failure status, output text, execution_id, execution_time_ms. On failure, returns error message.

### `execute_shell`

Execute a shell command in a sandbox (Ship container).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `sandbox_id` | string | Yes | — | Target sandbox |
| `command` | string | Yes | — | Shell command to execute |
| `cwd` | string | No | `/workspace` | Working directory, relative to `/workspace` |
| `timeout` | integer | No | 30 | Execution timeout in seconds (1-300) |
| `include_code` | boolean | No | false | Include command and metadata in response |
| `description` | string | No | — | Description for execution history |
| `tags` | string | No | — | Comma-separated tags |

**Returns**: Success/failure status, output, exit code, execution_id.

---

## File Operations

All paths are relative to `/workspace`.

### `read_file`

Read a file from the sandbox workspace.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `sandbox_id` | string | Yes | Target sandbox |
| `path` | string | Yes | File path relative to `/workspace` |

**Returns**: File content (auto-truncated at 12,000 chars).

### `write_file`

Write content to a file. Creates parent directories automatically.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `sandbox_id` | string | Yes | Target sandbox |
| `path` | string | Yes | File path relative to `/workspace` |
| `content` | string | Yes | Content to write (max 5MB UTF-8 encoded) |

### `list_files`

List files and directories in the sandbox workspace.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `sandbox_id` | string | Yes | — | Target sandbox |
| `path` | string | No | `.` | Directory path relative to `/workspace` |

**Returns**: List of entries with name, type (file/dir), and size.

### `delete_file`

Delete a file or directory from the sandbox workspace.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `sandbox_id` | string | Yes | Target sandbox |
| `path` | string | Yes | Path to delete, relative to `/workspace` |

### `upload_file`

Upload a local file to a sandbox workspace. Reads a file from the local filesystem (where the MCP server runs) and uploads it to the sandbox. Supports binary files (images, PDFs, archives, etc.).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `sandbox_id` | string | Yes | — | Target sandbox |
| `local_path` | string | Yes | — | Absolute or relative path to the local file. Relative paths are resolved from the MCP server's working directory. |
| `sandbox_path` | string | No | *(local filename)* | Target path in the sandbox workspace, relative to `/workspace`. If not provided, uses the local file's name. |

**Returns**: Local path, sandbox path, and file size in bytes.

**Constraints**:
- Max file size: 50MB (configurable via `SHIPYARD_MAX_TRANSFER_FILE_BYTES`)
- Local file must exist and be a regular file

### `download_file`

Download a file from a sandbox workspace to the local filesystem. Supports binary files (images, PDFs, archives, etc.).

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `sandbox_id` | string | Yes | — | Target sandbox |
| `sandbox_path` | string | Yes | — | File path in the sandbox, relative to `/workspace` |
| `local_path` | string | No | *(cwd/filename)* | Local destination path. Absolute or relative to the MCP server's working directory. Parent directories are created automatically. If not provided, saves to the current directory using the sandbox file's name. |

**Returns**: Sandbox path, local path, and file size in bytes.

**Constraints**:
- Max downloaded file size: 50MB (configurable via `SHIPYARD_MAX_TRANSFER_FILE_BYTES`)

---

## Browser Automation

**Important**: These tools execute in the **Gull container**, not Ship. Commands must NOT include the `agent-browser` prefix — it is injected automatically.

### `execute_browser`

Execute a single browser automation command.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `sandbox_id` | string | Yes | — | Target sandbox (must have browser capability) |
| `cmd` | string | Yes | — | Browser command without `agent-browser` prefix (e.g., `open https://example.com`, `snapshot -i`, `click @e1`) |
| `timeout` | integer | No | 30 | Execution timeout in seconds (1-300) |

**Returns**: Success/failure status, output, exit code.

### `execute_browser_batch`

Execute a sequence of browser commands in order within one request. Use for deterministic sequences that don't need intermediate reasoning.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `sandbox_id` | string | Yes | — | Target sandbox |
| `commands` | string[] | Yes | — | List of browser commands (without prefix). E.g., `["open https://example.com", "wait --load networkidle", "snapshot -i"]` |
| `timeout` | integer | No | 60 | Overall timeout in seconds for all commands (1-600) |
| `stop_on_error` | boolean | No | true | Stop execution if a command fails |

**Returns**: Per-step results (cmd, stdout, stderr, exit_code, duration_ms), total/completed step counts, overall success.

---

## Execution History

### `get_execution_history`

Query execution history for a sandbox with optional filters.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `sandbox_id` | string | Yes | — | Target sandbox |
| `exec_type` | string | No | — | Filter by type: `python`, `shell`, `browser`, `browser_batch` |
| `success_only` | boolean | No | false | Return only successful executions |
| `limit` | integer | No | 50 | Max entries to return (1-500) |
| `tags` | string | No | — | Comma-separated tags filter |
| `has_notes` | boolean | No | false | Return only entries that have notes |
| `has_description` | boolean | No | false | Return only entries that have description |

**Returns**: Total count, list of entries with id, exec_type, success, execution_time_ms, description, tags.

### `get_execution`

Get one full execution record by ID.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `sandbox_id` | string | Yes | Target sandbox |
| `execution_id` | string | Yes | Execution record ID |

**Returns**: Full record including id, type, success, time_ms, tags, description, notes, code, output, error.

### `get_last_execution`

Get the most recent execution record.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `sandbox_id` | string | Yes | — | Target sandbox |
| `exec_type` | string | No | — | Filter by type: `python`, `shell`, `browser`, `browser_batch` |

### `annotate_execution`

Add or update description/tags/notes for one execution record.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `sandbox_id` | string | Yes | Target sandbox |
| `execution_id` | string | Yes | Execution record ID |
| `description` | string | No | Description text |
| `tags` | string | No | Comma-separated tags |
| `notes` | string | No | Agent notes |

---

## Skill Lifecycle

### `create_skill_candidate`

Create a reusable skill candidate from execution IDs.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `skill_key` | string | Yes | Skill identifier (e.g., `etl-loader`, `web-scraper`) |
| `source_execution_ids` | string[] | Yes | Execution IDs used as source evidence |
| `scenario_key` | string | No | Optional scenario key (e.g., `csv-import`) |
| `payload_ref` | string | No | Optional payload reference |

**Returns**: Candidate ID, skill_key, status, source_execution_ids.

### `evaluate_skill_candidate`

Record evaluation result for a skill candidate.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `candidate_id` | string | Yes | Skill candidate ID |
| `passed` | boolean | Yes | Whether evaluation passed |
| `score` | number | No | Evaluation score (e.g., 0.0-1.0) |
| `benchmark_id` | string | No | Benchmark identifier |
| `report` | string | No | Evaluation report text |

### `promote_skill_candidate`

Promote a passing skill candidate to release.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `candidate_id` | string | Yes | — | Skill candidate ID |
| `stage` | string | No | `canary` | Release stage: `canary` or `stable` |

**Returns**: Release ID, skill_key, version, stage, active status.

### `list_skill_candidates`

List skill candidates with optional filters.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `status` | string | No | — | Status filter |
| `skill_key` | string | No | — | Skill key filter |
| `limit` | integer | No | 50 | Max items (1-500) |
| `offset` | integer | No | 0 | Pagination offset |

### `list_skill_releases`

List skill releases with optional filters.

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `skill_key` | string | No | — | Skill key filter |
| `active_only` | boolean | No | false | Only active releases |
| `stage` | string | No | — | Stage filter: `canary` or `stable` |
| `limit` | integer | No | 50 | Max items (1-500) |
| `offset` | integer | No | 0 | Pagination offset |

### `rollback_skill_release`

Rollback an active release to a previous known-good version.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `release_id` | string | Yes | Release ID to rollback from |

**Returns**: New release ID, skill_key, version, rollback_of.
