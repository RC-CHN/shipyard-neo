---
name: python-sandbox
description: "Ship runtime usage guide for code execution sandboxes. Covers Python execution via IPython kernel with persistent variables, Shell command execution, filesystem operations with security constraints, and common development workflows. Use this when working inside a Ship container to understand available tools, pre-installed libraries, path security rules, and best practices for file I/O, data processing, and project scaffolding."
---

# Ship Runtime: Python & Shell Sandbox

Ship is a containerized execution environment providing three core capabilities: **Python** (via IPython kernel), **Shell**, and **Filesystem** operations. All operations are confined to the `/workspace` directory.

## Container Base Image

- **Base**: `python:3.13-slim-bookworm` (Debian Bookworm minimal)
- **Runtime user**: `shipyard` (uid 1000, passwordless sudo)
- **Workspace**: `/workspace` (mounted as shared Cargo Volume)

## Architecture

```
┌────────────────────────────────────┐
│         Ship Container             │
│                                    │
│  ┌──────────┐  ┌───────────────┐   │
│  │  IPython  │  │  Shell/Bash   │   │
│  │  Kernel   │  │  Executor     │   │
│  └─────┬─────┘  └──────┬───────┘   │
│        └───────┬────────┘           │
│         ┌──────┴──────┐            │
│         │  Filesystem  │            │
│         │  /workspace  │            │
│         └─────────────┘            │
└────────────────────────────────────┘
```

## Capability 1: Python Execution

Python code runs inside an **IPython kernel**. Variables, imports, and state persist across calls within the same sandbox session.

### Key Behaviors

- **Persistent state**: Variables defined in one execution are available in subsequent ones
- **Working directory**: `/workspace` (all relative paths resolve here)
- **Output**: stdout/stderr are captured and returned; the last expression value is also captured
- **Timeout**: Default 30s, max 300s

### Common Patterns

**Data processing pipeline**:

```python
# Call 1: Load data
import pandas as pd
df = pd.read_csv("data.csv")
print(f"Loaded {len(df)} rows")

# Call 2: Process (df persists from call 1)
summary = df.describe()
summary.to_csv("summary.csv")
print("Summary saved")
```

**Visualization**:

```python
import matplotlib.pyplot as plt
import matplotlib
matplotlib.use("Agg")  # Non-interactive backend

fig, ax = plt.subplots(figsize=(10, 6))
ax.plot(df["date"], df["value"])
ax.set_title("Trend")
fig.savefig("chart.png", dpi=150, bbox_inches="tight")
plt.close(fig)
print("Chart saved to chart.png")
```

**Install additional packages** (when pre-installed ones are insufficient):

```python
import subprocess
subprocess.run(["pip", "install", "package-name"], check=True)
```

### Pre-installed Python Libraries

| Category | Libraries |
|----------|-----------|
| Data Science | numpy, pandas, scikit-learn, scipy, matplotlib, seaborn |
| Image Processing | Pillow, opencv-python-headless, imageio |
| Document Processing | python-docx, python-pptx, openpyxl, xlrd, xlsxwriter, pypdf, pdfplumber, reportlab |
| Web/HTTP | beautifulsoup4, lxml, jinja2, httpx, requests |
| Utilities | pydantic, tomli, aiofiles, tenacity, cachetools, tqdm, orjson, python-slugify |
| IPython/Jupyter | ipython, ipykernel, jupyter-client |
| Monitoring | sentry-sdk |

> **Tip**: Additional packages can be installed at runtime via `pip install`. Installed packages do not persist across container restarts (unless you save a requirements.txt to `/workspace` and re-install).

### Language Runtimes

| Runtime | Version | Details |
|---------|---------|---------|
| Python | 3.13 | IPython kernel; variables persist across calls |
| Node.js | LTS | npm, pnpm, vercel globally installed |

### System-level Packages

| Category | Packages |
|----------|----------|
| Version Control | git |
| HTTP/Network | curl |
| Text Editors | vim-tiny, nano, less |
| Process Management | procps, htop |
| Permissions | sudo (passwordless for shipyard user) |
| Image/Font Libs | libpng16-16, libjpeg62-turbo, fontconfig |
| CJK Fonts | fonts-noto-cjk |
| Emoji/Symbol | fonts-symbola |
| XML Libs | libxml2, libxslt1.1 |

### CJK Font Support

Noto Sans CJK and Symbola fonts are pre-installed. Matplotlib font cache is pre-warmed. For CJK text in plots:

```python
import matplotlib.pyplot as plt
plt.rcParams["font.sans-serif"] = ["Noto Sans CJK SC", "DejaVu Sans"]
plt.rcParams["axes.unicode_minus"] = False
```

## Capability 2: Shell Execution

Shell commands run as the `shipyard` user with passwordless `sudo` available.

### Key Behaviors

- **Shell**: `/bin/bash`
- **User**: `shipyard` (uid 1000) with `sudo NOPASSWD: ALL`
- **Working directory**: Relative paths use `/workspace`; allowed absolute paths may use `/workspace` or `/tmp`
- **Chaining**: Shell operators (`&&`, `|`, `;`, `>`) work as expected

### Common Patterns

**Project scaffolding**:

```bash
mkdir -p src tests docs
echo '# My Project' > README.md
git init && git add .
```

**Node.js development** (Node LTS + npm + pnpm + vercel are pre-installed):

```bash
npm init -y && npm install express
# or
pnpm init && pnpm add express
```

**System operations**:

```bash
# Check running processes
ps aux

# Disk usage
du -sh /workspace/*

# Install system packages (sudo available)
sudo apt-get update && sudo apt-get install -y <package>
```

**Long-running/background tasks**:

```bash
# Run a process in background
python server.py &
echo $!  # Print PID for later management
```

### System Tools Available

`git`, `curl`, `vim-tiny`, `nano`, `less`, `htop`, `procps`, `sudo`

## Capability 3: Filesystem Operations

Direct file read/write/list/delete operations without shell.

### Key Behaviors

- Relative paths are rooted at `/workspace`; allowed absolute paths may use `/workspace` or `/tmp`
- `/tmp` is local to the current Ship container and is not shared or persistent across restarts
- Path traversal outside the configured allowed roots is blocked with HTTP 403
- Parent directories are created automatically on write

### Security Constraint

**All file paths must resolve to within `/workspace`**. The following patterns are rejected:

- `../etc/passwd` → **blocked** (traverses outside workspace)
- `/etc/passwd` → **blocked** (absolute path outside workspace)
- `subdir/../../etc/passwd` → **blocked** (resolved path escapes workspace)

Safe patterns:

- `src/main.py` → `/workspace/src/main.py` ✅
- `./output/data.csv` → `/workspace/output/data.csv` ✅
- `/workspace/src/main.py` → `/workspace/src/main.py` ✅

## Common Workflows

### 1. Write → Execute → Save Results

```
write_file("script.py", code)  →  execute_python(code)  →  download("result.csv")
```

### 2. Data Processing Pipeline

```
upload("raw_data.csv")
  → execute_python: load + clean + analyze
  → execute_python: generate visualization
  → download("chart.png")
```

### 3. Cross-Container Collaboration (Multi-Container Profiles)

When running in a multi-container sandbox (e.g., `browser-python` profile), Ship shares `/workspace` with the Gull (browser) container:

```
Gull: screenshot /workspace/page.png    → writes to shared volume
Ship: Image.open("page.png")           → reads from shared volume
Ship: open("report.html", "w").write() → writes to shared volume
Gull: open file:///workspace/report.html → reads from shared volume
```

### 4. Project Build & Deploy

```bash
# Clone, install, build, and serve
git clone https://github.com/user/project.git
cd project && npm install && npm run build
# Output is in /workspace/project/dist/
```

## Environment Variables

Profile-level environment variables can be configured in Bay's `config.yaml` and are automatically injected into the sandbox runtime:

```yaml
profiles:
  - id: python-default
    image: "ship:latest"
    env:
      API_KEY: "secret-key-12345"
      DEBUG_MODE: "true"
      DATABASE_URL: "postgresql://localhost/mydb"
```

### How It Works

1. **Container Startup**: Bay encodes `profile.env` as `BAY_PROFILE_ENV` (format: `KEY1=VALUE1:KEY2=VALUE2`)
2. **Entrypoint Processing**: Ship's entrypoint.sh parses `BAY_PROFILE_ENV` and generates `/workspace/.bay_env.sh`
3. **Runtime Injection**:
   - **Shell**: Commands automatically source `/workspace/.bay_env.sh` before execution
   - **Python**: IPython kernel loads environment variables at startup

### Accessing Environment Variables

**In Shell**:
```bash
echo $API_KEY
# Output: secret-key-12345
```

**In Python**:
```python
import os
print(os.environ.get("API_KEY"))
# Output: secret-key-12345
```

### Persistence Notes

- Environment variables persist for the lifetime of the sandbox
- Changes to `profile.env` require creating a new sandbox
- Users can manually add custom env vars to `/workspace/.bashrc` for persistence across shell sessions

## Constraints Summary

| Constraint | Value |
|------------|-------|
| Workspace root | `/workspace` |
| Path security | All paths must resolve within `/workspace` |
| Python timeout | 1-300s (default 30s) |
| Shell timeout | 1-300s (default 30s) |
| Write file limit | 5MB (UTF-8 encoded) |
| Upload/download limit | 50MB per file |
| Python runtime | 3.13 (IPython kernel, variables persist) |
| Node.js runtime | LTS (npm, pnpm, vercel) |
| Shell user | `shipyard` with passwordless sudo |
| Profile env vars | Automatically injected via `/workspace/.bay_env.sh` |
