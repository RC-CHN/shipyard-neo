# Sandbox Environment Details

A sandbox's container topology depends on the **profile** used to create it. Some profiles use a single container, others use multiple containers sharing a Cargo Volume.

## Discovering Profiles at Runtime

Call `list_profiles` to discover available profiles and their container topology:

```
list_profiles(reason="Discover available profiles")
```

**Example output** (with container detail):

```
Available Profiles (5)

- python-default: capabilities=[filesystem, shell, python], idle_timeout=3600s
    └ primary (ship): [filesystem, python, shell]
- browser-python — Browser automation with Python backend: capabilities=[browser, filesystem, python, shell], idle_timeout=3600s
    └ ship (ship): [filesystem, python, shell]
    └ browser (gull): [browser]
```

Key observations:
- `python-default` has **one container** (`primary` of type `ship`) — only Python/Shell/Filesystem operations available
- `browser-python` has **two containers** (`ship` + `browser` of type `gull`) — both code execution and browser automation available
- If `browser` capability is not in the profile's capabilities list, `execute_browser` and `execute_browser_batch` will fail

## Example: Single-Container Profile (`python-default`)

```
┌──────────────────────────────────────┐
│           Sandbox                    │
│  ┌────────────────────────────────┐  │
│  │       Ship Container           │  │
│  │  Python 3.13 + IPython         │  │
│  │  Node.js LTS + pnpm            │  │
│  │  git, curl, vim, sudo, etc.    │  │
│  │  Data science + doc libraries  │  │
│  └──────────┬─────────────────────┘  │
│             │                        │
│  ┌──────────┴─────────────────────┐  │
│  │       Cargo Volume             │  │
│  │       /workspace               │  │
│  └────────────────────────────────┘  │
└──────────────────────────────────────┘
```

**Available MCP tools**: `execute_python`, `execute_shell`, `read_file`, `write_file`, `list_files`, `delete_file`

**Not available**: `execute_browser`, `execute_browser_batch` (no Gull container)

## Example: Multi-Container Profile (`browser-python`)

```
┌──────────────────────────────────────────────────────────┐
│                    Sandbox                                │
│  ┌──────────────────┐      ┌──────────────────┐          │
│  │  Ship Container   │      │  Gull Container   │         │
│  │  (code execution) │      │ (browser automat.)│         │
│  │                   │      │                   │         │
│  │  Python, Shell,   │      │  agent-browser    │         │
│  │  Filesystem       │      │  Chromium headless│         │
│  └────────┬──────────┘      └────────┬──────────┘         │
│           │                          │                    │
│           └──────────┬───────────────┘                    │
│                      │                                    │
│           ┌──────────┴──────────┐                         │
│           │   Cargo Volume      │                         │
│           │   /workspace        │                         │
│           └─────────────────────┘                         │
└──────────────────────────────────────────────────────────┘
```

**Available MCP tools**: All tools — code execution + browser automation + file operations

**Critical rules for multi-container profiles**:
- **Never run `agent-browser` in `execute_shell`** — Ship does not have agent-browser
- **Never add `agent-browser` prefix to `execute_browser` cmd** — Gull auto-injects it
- Both containers share `/workspace` via the Cargo Volume

## Ship Container Environment

Ship containers are based on `python:3.13-slim-bookworm`.

### Language Runtimes

| Runtime | Details |
|---------|---------|
| **Python 3.13** | IPython kernel; variables persist across `execute_python` calls |
| **Node.js LTS** | npm, pnpm, vercel globally installed |

### Pre-installed Python Libraries

| Category | Libraries |
|----------|-----------|
| Data Science | numpy, pandas, scikit-learn, matplotlib, seaborn |
| Image Processing | Pillow, opencv-python-headless, imageio |
| Document Processing | python-docx, python-pptx, openpyxl, xlrd, pypdf, pdfplumber, reportlab |
| Web/XML | beautifulsoup4, lxml, jinja2 |
| Utilities | pydantic, tomli, aiofiles |

### System Tools

`git`, `curl`, `vim-tiny`, `nano`, `less`, `htop`, `procps`, `sudo` (passwordless)

### Fonts

CJK fonts (Noto Sans CJK) and Symbola pre-installed. Matplotlib font cache pre-warmed.

## Gull Container Environment

Gull containers provide browser automation via the `agent-browser` CLI.

### Auto-injected Parameters

| Parameter | Value | Purpose |
|-----------|-------|---------|
| `--session` | Sandbox ID | Browser instance isolation |
| `--profile` | `/workspace/.browser/profile/` | Persistent browser state |

### Browser State Persistence

Cookies, localStorage, IndexedDB, service workers, and cache are persisted to `/workspace/.browser/profile/` on the Cargo Volume. State survives container restarts and is cleaned up on sandbox deletion.

## Cargo Volume

| Property | Details |
|----------|---------|
| Mount path | `/workspace` |
| Shared between | All containers in the same sandbox |
| Default size limit | 1024 MB |
| Persistence | Survives container restarts; deleted with sandbox |

### Cross-Container File Access

```
# Gull writes screenshot → Ship reads it
execute_browser(cmd="screenshot /workspace/page.png")
execute_python(code="from PIL import Image; img = Image.open('page.png')")

# Ship writes file → Gull opens it
execute_python(code="open('report.html', 'w').write(html)")
execute_browser(cmd="open file:///workspace/report.html")
```
