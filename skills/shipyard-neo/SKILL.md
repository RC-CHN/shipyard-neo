---
name: shipyard-neo
description: "Shipyard Neo sandbox MCP tools usage guide. This skill should be used when the user needs to execute code in sandboxes, manage files in sandbox workspaces, automate browsers in sandboxes, manage execution history, or work with skill lifecycle (candidates, evaluations, releases, rollbacks) through the shipyard-neo MCP server. Triggers include requests to 'run code in a sandbox', 'create a sandbox', 'execute Python/Shell in sandbox', 'automate browser in sandbox', 'manage skills', 'check execution history', or any task involving the shipyard-neo MCP tools."
---

# Shipyard Neo Sandbox MCP Tools

Shipyard Neo provides isolated sandbox environments for executing Python, Shell, browser automation, and file operations through MCP tools. All MCP tools are prefixed with `mcp--shipyard___neo--`.

## Architecture Overview

A sandbox's container topology depends on the **profile** used to create it. Call `list_profiles` to discover available profiles and their container layout.

### Single-Container Profile (e.g., `python-default`)

Only a **Ship container** — supports Python, Shell, and Filesystem operations. No browser capability.

### Multi-Container Profile (e.g., `browser-python`)

```
┌──────────────────────────────────────────────────────────────┐
│                        Sandbox                               │
│  ┌──────────────────┐      ┌──────────────────┐              │
│  │  Ship Container   │      │  Gull Container   │             │
│  │  (code execution) │      │ (browser automat.)│             │
│  │  Python, Shell,   │      │  agent-browser    │             │
│  │  Filesystem       │      │  Chromium headless│             │
│  └────────┬──────────┘      └────────┬──────────┘             │
│           └──────────┬───────────────┘                        │
│           ┌──────────┴──────────┐                             │
│           │   Cargo Volume      │                             │
│           │   /workspace        │                             │
│           └─────────────────────┘                             │
└──────────────────────────────────────────────────────────────┘
```

### Container Isolation Rules

| Container | Responsibility | MCP Tools | Cannot Do |
|-----------|---------------|-----------|-----------|
| **Ship** | Python / Shell / Filesystem | `execute_python`, `execute_shell`, `read_file`, `write_file`, `upload_file`, `download_file`, `list_files`, `delete_file` | No `agent-browser` installed — cannot run browser commands |
| **Gull** | Browser automation | `execute_browser`, `execute_browser_batch` | No Python/Shell — cannot execute code |

**Critical rules**:

- **Never run `agent-browser` commands in `execute_shell`** — Ship container does not have agent-browser installed
- **Never prefix `execute_browser` commands with `agent-browser`** — Gull auto-injects it; duplicating causes `agent-browser agent-browser ...` error
- Both containers share the **Cargo Volume** at `/workspace`

### Cross-Container Data Sharing

Both containers exchange files through the shared `/workspace` volume:

```
# Browser screenshot → Python processing
execute_browser(cmd="screenshot /workspace/page.png")  → Gull writes file
read_file(path="page.png")                             → Ship reads file
execute_python(code="from PIL import Image; img = Image.open('page.png')")

# Python generates data → Browser uses it
execute_python(code="with open('data.json', 'w') as f: json.dump(data, f)")
execute_browser(cmd="open file:///workspace/report.html")
```

## Ship Container Pre-installed Environment

Ship container is based on `python:3.13-slim-bookworm` with rich pre-installed tools. See [references/sandbox-environment.md](references/sandbox-environment.md) for details.

### Language Runtimes

| Runtime | Details |
|---------|---------|
| **Python 3.13** | Executed via IPython kernel; variables persist across calls within same sandbox |
| **Node.js LTS** | Includes npm, pnpm, vercel |

### Pre-installed Python Libraries

| Category | Libraries |
|----------|-----------|
| Data Science | numpy, pandas, scikit-learn, matplotlib, seaborn |
| Image Processing | Pillow, opencv-python-headless, imageio |
| Document Processing | python-docx, python-pptx, openpyxl, xlrd, pypdf, pdfplumber, reportlab |
| Web/XML | beautifulsoup4, lxml, jinja2 |
| Utilities | tomli, pydantic |

### System Tools

`git`, `curl`, `vim-tiny`, `nano`, `less`, `htop`, `procps`, `sudo`

## Core Workflows

### 1. Sandbox Lifecycle

```
list_profiles → create_sandbox → [operations] → delete_sandbox
```

1. Call `list_profiles` to discover available profiles (e.g., `python-default` for Ship only, `browser-python` for Ship + Gull)
2. Call `create_sandbox` to create a sandbox and obtain `sandbox_id`
3. Use `sandbox_id` for all subsequent operations
4. Call `delete_sandbox` when finished to release resources

### 2. Code Execution

**Python** (via IPython — variables persist across calls):

```python
# First call
execute_python(sandbox_id="xxx", code="import pandas as pd; df = pd.read_csv('data.csv')")

# Subsequent call can use df directly — variables persist within the same sandbox
execute_python(sandbox_id="xxx", code="print(df.describe())")
```

**Shell**:

```python
execute_shell(sandbox_id="xxx", command="ls -la", cwd="src")
execute_shell(sandbox_id="xxx", command="npm install && npm run build")
execute_shell(sandbox_id="xxx", command="git init && git add .")
```

### 3. File Operations

All sandbox paths are **relative to `/workspace`**:

```python
# Text file operations (content passed through MCP protocol)
write_file(sandbox_id="xxx", path="src/main.py", content="print('hello')")
read_file(sandbox_id="xxx", path="src/main.py")
list_files(sandbox_id="xxx", path="src")
delete_file(sandbox_id="xxx", path="src/temp.py")

# Binary/local file transfer (reads/writes actual files on the MCP server's filesystem)
upload_file(sandbox_id="xxx", local_path="/path/to/data.csv", sandbox_path="data/input.csv")
download_file(sandbox_id="xxx", sandbox_path="output/result.png", local_path="./result.png")
```

**When to use `upload_file`/`download_file` vs `write_file`/`read_file`**:

| Scenario | Use |
|----------|-----|
| Writing text/code content inline | `write_file` |
| Reading text file content for analysis | `read_file` |
| Uploading existing local files (images, datasets, archives) | `upload_file` |
| Downloading binary outputs (images, PDFs, compiled artifacts) | `download_file` |
| Large files (>5MB text or any binary) | `upload_file`/`download_file` |

### 4. Browser Automation

Browser commands execute in the Gull container. **Do NOT add the `agent-browser` prefix.**

Operational model (do this to avoid flaky runs):

- Treat `cmd` as a single command line split into args and executed directly (not a shell script).
- Orchestrate branching/loops in the agent logic, not inside `cmd`.
- Always follow the cadence: **Navigate → Snapshot → Interact → Wait → Re-snapshot**.

**Standard workflow**:

1. `execute_browser(cmd="open https://example.com")` — Navigate
2. `execute_browser(cmd="wait --load networkidle")` — Stabilize (optional but recommended)
3. `execute_browser(cmd="snapshot -i")` — Get interactive element refs (`@e1`, `@e2`, ...)
4. Analyze snapshot output to determine next action
5. `execute_browser(cmd="click ..." | "fill ..." | "select ...")` — Interact using refs
6. If the DOM may have changed, run `execute_browser(cmd="snapshot -i")` again before further interactions

**When to use single vs batch**:

| Scenario | Recommended |
|----------|-------------|
| Need intermediate reasoning (snapshot → analyze → decide) | Multiple single `execute_browser` calls |
| Deterministic sequence (open → fill → click → wait) | `execute_browser_batch` |
| Complex conditional flows (login, error recovery) | Agent orchestrates multiple single calls |

Read [`skills/shipyard-neo/references/browser.md`](skills/shipyard-neo/references/browser.md:1) for the detailed command reference, patterns, and artifact handling.

### 5. Execution History

Track and retrieve past executions for debugging, auditing, or skill creation:

- `get_execution_history` — Query with filters (`exec_type`, `success_only`, `tags`, `limit`)
- `get_execution` — Get full details of one execution by ID
- `get_last_execution` — Get the most recent execution
- `annotate_execution` — Add/update `description`, `tags`, `notes`

### 6. Skill Self-Update Lifecycle

Turn proven execution patterns into reusable, versioned skills.

This mechanism separates **evidence** from **release decisions**: execution history records what actually happened, while candidate/evaluation/release records what should be reused.
Use optional metadata fields (`summary`, `usage_notes`, `preconditions`, `postconditions`, `upgrade_reason`, `change_summary`) to make each iteration explainable and auditable.

1. Execute tasks → collect `execution_id`s
2. `annotate_execution` — Tag and describe executions
3. `create_skill_candidate` — Bundle execution IDs into a candidate; optionally include `summary`, `usage_notes`, `preconditions`, `postconditions` for explainable skill metadata
4. `evaluate_skill_candidate` — Record evaluation results (pass/fail, score, report)
5. `promote_skill_candidate` — Release as `canary` or `stable`; optionally include `upgrade_of_release_id`, `upgrade_reason`, `change_summary` for upgrade traceability
6. `rollback_skill_release` — Revert to previous version if needed
7. `delete_skill_release` / `delete_skill_candidate` — Soft-delete stale records (only inactive release can be deleted; candidate referenced by active release cannot be deleted)

See [references/skills-lifecycle.md](references/skills-lifecycle.md) for the complete workflow, including optional metadata fields for candidate creation and release promotion.

## Key Constraints

| Constraint | Value |
|------------|-------|
| sandbox_id format | 1-128 chars, only `[a-zA-Z0-9_-]` |
| Execution timeout | Single: 1-300s (default 30); Batch: 1-600s (default 60) |
| Output truncation | Auto-truncated beyond 12,000 characters |
| write_file limit | 5MB max (UTF-8 encoded) |
| upload/download limit | 50MB max per file |
| Browser prefix | **Never** include `agent-browser` prefix |
| Ref lifecycle | Invalidated after page navigation or DOM changes; always re-snapshot |
| Container isolation | Ship cannot run browser commands; Gull cannot run Python/Shell |

## Deep-Dive Documentation

| Reference | When to Use |
|-----------|-------------|
| [references/tools-reference.md](references/tools-reference.md) | Full parameter reference for all 21 MCP tools |
| [references/browser.md](references/browser.md) | Browser automation commands, patterns, and troubleshooting |
| [references/skills-lifecycle.md](references/skills-lifecycle.md) | Skill candidate → evaluate → promote → rollback workflow |
| [references/sandbox-environment.md](references/sandbox-environment.md) | Ship/Gull container pre-installed environment and capability details |
