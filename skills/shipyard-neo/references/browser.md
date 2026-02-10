# Browser Automation Guide

Browser automation in Shipyard Neo runs in the **Gull container**, separate from the Ship container (Python/Shell). All commands execute via the `execute_browser` and `execute_browser_batch` MCP tools.

## Critical Rules

1. **Never include `agent-browser` prefix** — Gull auto-injects it. Sending `agent-browser open ...` becomes `agent-browser agent-browser open ...` and fails.
2. **Never use `execute_shell` for browser commands** — Ship container does not have agent-browser installed.
3. **Always re-snapshot after DOM changes** — Element refs (`@e1`, `@e2`) are invalidated by navigation, form submissions, and dynamic content loading.
4. **Sandbox must have browser capability** — Use a browser-enabled profile like `browser-python`.

## Core Workflow

Every browser automation follows the **Navigate → Snapshot → Interact → Re-snapshot** cycle:

```
execute_browser(cmd="open https://example.com/form")
execute_browser(cmd="snapshot -i")
# Output shows: @e1 [input type="email"], @e2 [input type="password"], @e3 [button] "Submit"

execute_browser(cmd="fill @e1 \"user@example.com\"")
execute_browser(cmd="fill @e2 \"password123\"")
execute_browser(cmd="click @e3")
execute_browser(cmd="wait --load networkidle")
execute_browser(cmd="snapshot -i")    # MUST re-snapshot — refs are now invalid
```

## Command Reference

### Navigation

```
open <url>                    Navigate to URL (aliases: goto, navigate)
close                         Close browser
```

### Snapshot

```
snapshot -i                   Interactive elements with refs (recommended)
snapshot -i -C                Include cursor-interactive elements (divs with onclick, cursor:pointer)
snapshot -s "#selector"       Scope to CSS selector
snapshot -i --json            JSON output for parsing
```

### Interaction (use @refs from snapshot)

```
click @e1                     Click element
fill @e2 "text"               Clear and type text
type @e2 "text"               Type without clearing
select @e1 "option"           Select dropdown option
check @e1                     Check checkbox
press Enter                   Press key
scroll down 500               Scroll page
```

### Get Information

```
get text @e1                  Get element text
get text @e1 --json           Get text as JSON
get url                       Get current URL
get title                     Get page title
```

### Wait

```
wait @e1                      Wait for element to appear
wait --load networkidle       Wait for network idle
wait --url "**/page"          Wait for URL pattern
wait 2000                     Wait milliseconds
```

### Capture

```
screenshot                    Screenshot to temp dir
screenshot /workspace/pg.png  Screenshot to specific path
screenshot --full             Full page screenshot
pdf output.pdf                Save as PDF
```

### Semantic Locators (Alternative to Refs)

When refs are unavailable or unreliable:

```
find text "Sign In" click
find label "Email" fill "user@test.com"
find role button click --name "Submit"
find placeholder "Search" type "query"
find testid "submit-btn" click
```

## Ref Lifecycle

Refs (`@e1`, `@e2`, etc.) are **ephemeral**. They become invalid when:

- Clicking links or buttons that cause navigation
- Form submissions
- Dynamic content loading (dropdowns, modals, AJAX updates)
- Page refresh

**Always re-snapshot after any action that changes the DOM**:

```
execute_browser(cmd="click @e5")     # Navigates to new page
execute_browser(cmd="snapshot -i")   # MUST re-snapshot to get fresh refs
execute_browser(cmd="click @e1")     # Use new refs
```

## Common Patterns

### Form Submission

```
execute_browser(cmd="open https://example.com/signup")
execute_browser(cmd="snapshot -i")
# Analyze snapshot output to identify form fields
execute_browser(cmd="fill @e1 \"Jane Doe\"")
execute_browser(cmd="fill @e2 \"jane@example.com\"")
execute_browser(cmd="select @e3 \"California\"")
execute_browser(cmd="check @e4")
execute_browser(cmd="click @e5")
execute_browser(cmd="wait --load networkidle")
execute_browser(cmd="snapshot -i")    # Verify submission result
```

### Authentication Flow

Agent orchestrates the login flow with intermediate reasoning:

```
# Step 1: Navigate to target page
execute_browser(cmd="open https://app.example.com/dashboard")
execute_browser(cmd="snapshot -i")

# Step 2: Agent analyzes snapshot
#   → If "Welcome" and username visible → already logged in → proceed
#   → If "Sign In" form visible → execute login flow:

execute_browser(cmd="fill @e1 \"user@example.com\"")
execute_browser(cmd="fill @e2 \"password\"")
execute_browser(cmd="click @e3")
execute_browser(cmd="wait --load networkidle")
execute_browser(cmd="snapshot -i")

# Step 3: Agent verifies login result
#   → Success → continue task
#   → Failure → analyze reason (captcha? wrong password?) and adapt
```

### Data Extraction

```
execute_browser(cmd="open https://example.com/products")
execute_browser(cmd="snapshot -i")
execute_browser(cmd="get text @e5")              # Get specific element text
execute_browser(cmd="get text body")             # Get all page text
execute_browser(cmd="snapshot -i --json")         # JSON output for structured parsing
```

### Screenshot and Cross-Container Processing

Screenshots are written to the shared `/workspace` volume, accessible from Ship:

```
# Gull takes screenshot
execute_browser(cmd="screenshot /workspace/page.png")

# Ship processes it
execute_python(code="""
from PIL import Image
img = Image.open('page.png')
print(f'Image size: {img.size}')
""")
```

### Deterministic Batch Execution

For sequences that don't need intermediate reasoning:

```
execute_browser_batch(
    commands=[
        "open https://example.com",
        "wait --load networkidle",
        "snapshot -i"
    ],
    timeout=60,
    stop_on_error=true
)
```

Another example — filling a form when element positions are known:

```
execute_browser_batch(
    commands=[
        "open https://example.com/form",
        "wait --load networkidle",
        "fill @e1 \"Jane Doe\"",
        "fill @e2 \"jane@example.com\"",
        "click @e3",
        "wait --load networkidle"
    ]
)
```

**Warning**: Batch execution does not return intermediate snapshots. If uncertain about element refs, use individual `execute_browser` calls with `snapshot -i` between interactions.

## Single vs Batch Decision Guide

| Scenario | Approach | Reason |
|----------|----------|--------|
| First visit to a page | Single: `open` → `snapshot -i` | Need to see page structure before interacting |
| Known form with stable refs | Batch: `fill` → `fill` → `click` | Deterministic, saves round trips |
| Login with unknown form structure | Single: multiple calls with snapshots | Need intermediate analysis |
| Page navigation + verification | Single: `click` → `snapshot -i` | Must re-snapshot after navigation |
| Scraping multiple items | Single: `snapshot` → `get text` per item | Need to analyze each result |
| Open page + wait + screenshot | Batch: `open` → `wait` → `screenshot` | Deterministic sequence |

## Browser State Persistence

Gull automatically persists browser state (cookies, localStorage, IndexedDB, service workers, cache) to `/workspace/.browser/profile/` on the Cargo Volume. This state:

- Survives container restarts within the same sandbox
- Is automatically cleaned up when the sandbox is deleted
- Enables session reuse across multiple `execute_browser` calls

## Error Handling

Common error patterns and solutions:

| Error | Cause | Solution |
|-------|-------|----------|
| `agent-browser agent-browser ...` | Included `agent-browser` prefix | Remove prefix from `cmd` parameter |
| `Element not found: @e5` | Ref invalidated by DOM change | Re-snapshot with `snapshot -i` |
| `Command timed out after Ns` | Page/element load too slow | Increase `timeout` parameter |
| `agent-browser not found` | Wrong container (Shell on Ship) | Use `execute_browser`, not `execute_shell` |
| `Sandbox does not have browser capability` | Profile lacks browser support | Use `browser-python` profile |
