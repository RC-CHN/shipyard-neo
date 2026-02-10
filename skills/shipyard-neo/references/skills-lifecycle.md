# Skill Self-Update Lifecycle

Shipyard Neo provides infrastructure for turning proven execution patterns into reusable, versioned skills. This document covers the complete lifecycle: evidence collection → candidate creation → evaluation → release → rollback.

## Overview

```
Execute tasks          Annotate         Create           Evaluate         Promote          (Rollback)
in sandbox      →    executions    →   candidate    →   candidate    →   release     →   if needed
(execution_ids)       (tags/desc)      (skill_key)     (pass/fail)     (canary/stable)
```

Shipyard Neo provides the **self-update infrastructure**, not a fixed training framework:

- **Runtime execution evidence layer**: Auto-records Python/Shell/Browser execution history
- **Skill control plane**: Candidate → Evaluation → Release → Rollback
- **Multiple entry points**: REST API / Python SDK / MCP tools

Whether to do online learning, offline evaluation, or A/B release strategies is determined by the upstream Agent system.

## Step-by-Step Workflow

### Step 1: Execute Tasks and Collect Evidence

Execute tasks normally in a sandbox. Each execution returns an `execution_id`:

```
# Execute Python with description and tags for later retrieval
execute_python(
    sandbox_id="xxx",
    code="import pandas as pd; df = pd.read_csv('data.csv'); print(df.head())",
    description="Load CSV data",
    tags="etl,data-loading"
)
# Returns: execution_id = "exec-abc123"

execute_python(
    sandbox_id="xxx",
    code="df_clean = df.dropna(); df_clean.to_csv('clean.csv')",
    description="Clean and export data",
    tags="etl,data-cleaning"
)
# Returns: execution_id = "exec-def456"
```

### Step 2: Annotate Executions (Optional but Recommended)

Add description, tags, and notes to executions for better traceability:

```
annotate_execution(
    sandbox_id="xxx",
    execution_id="exec-abc123",
    description="Load and validate CSV input",
    tags="etl,data-loading,validated",
    notes="Handles missing values and type coercion"
)
```

**Tag conventions** (recommended):

- Use consistent tag vocabulary: `etl`, `planner`, `retrieval`, `web-scraper`, `stable`
- Comma-separated, no spaces in individual tags
- Tags are searchable via `get_execution_history(tags="etl")`

### Step 3: Query Execution History

Review executions before creating a skill candidate:

```
# Get all executions tagged with "etl"
get_execution_history(
    sandbox_id="xxx",
    tags="etl",
    success_only=true
)

# Get full details of a specific execution
get_execution(sandbox_id="xxx", execution_id="exec-abc123")

# Get the most recent Python execution
get_last_execution(sandbox_id="xxx", exec_type="python")
```

### Step 4: Create Skill Candidate

Bundle a set of execution IDs into a skill candidate:

```
create_skill_candidate(
    skill_key="etl-csv-loader",
    source_execution_ids=["exec-abc123", "exec-def456"],
    scenario_key="csv-import"
)
# Returns: candidate_id = "cand-xyz789", status = "pending"
```

**Parameters**:

- `skill_key`: Unique identifier for the skill (e.g., `etl-csv-loader`, `web-scraper-v2`)
- `source_execution_ids`: Array of execution IDs that serve as evidence/source for this skill
- `scenario_key` (optional): Identifies the scenario this candidate covers (e.g., `csv-import`, `json-api`)
- `payload_ref` (optional): Reference to an external payload

### Step 5: Evaluate Skill Candidate

Record evaluation results for the candidate:

```
evaluate_skill_candidate(
    candidate_id="cand-xyz789",
    passed=true,
    score=0.95,
    benchmark_id="bench-etl-001",
    report="All test cases passed. CSV parsing handles edge cases correctly."
)
```

**Best practices**:

- Always evaluate before promoting — never promote an unevaluated candidate
- Include a meaningful `report` for auditability
- Use consistent `benchmark_id` values for reproducibility

### Step 6: Promote to Release

Promote a passing candidate to release:

```
# Start with canary
promote_skill_candidate(
    candidate_id="cand-xyz789",
    stage="canary"
)
# Returns: release_id = "rel-001", version = 1, stage = "canary", active = true

# Later, promote to stable after canary validation
promote_skill_candidate(
    candidate_id="cand-xyz789",
    stage="stable"
)
```

**Release stages**:

| Stage | Purpose |
|-------|---------|
| `canary` | Limited rollout for validation. Start here. |
| `stable` | Full production release. Promote after canary succeeds. |

### Step 7: Monitor and Rollback (If Needed)

List active releases and rollback if issues arise:

```
# List active releases for a skill
list_skill_releases(
    skill_key="etl-csv-loader",
    active_only=true
)

# Rollback to previous version
rollback_skill_release(release_id="rel-001")
# Returns: new_release_id, version, rollback_of = "rel-001"
```

## Query Tools

### List Candidates

```
# All candidates for a skill
list_skill_candidates(skill_key="etl-csv-loader")

# Filter by status
list_skill_candidates(status="pending")

# Paginate
list_skill_candidates(limit=10, offset=20)
```

### List Releases

```
# All releases for a skill
list_skill_releases(skill_key="etl-csv-loader")

# Only active releases
list_skill_releases(active_only=true)

# Filter by stage
list_skill_releases(stage="stable")
```

## Recommended Practices

1. **Tag standardization**: Maintain a consistent tag vocabulary across the team
2. **Evaluate before promote**: Never directly promote an unevaluated candidate
3. **Staged rollout**: Always start with `canary`, then promote to `stable`
4. **Rollback automation**: Bind critical metrics to rollback triggers
5. **Evidence traceability**: Candidates must retain `source_execution_ids` for audit
6. **Meaningful annotations**: Use `description` and `notes` to capture reasoning and context

## Built-in Stability Guarantees

1. **SDK retry strategy**: `GET/PUT/DELETE` auto-retry; `POST` retries only with `idempotency_key`
2. **Error semantics preserved**: Non-JSON error pages mapped to semantic exceptions (e.g., `NotFoundError`)
3. **MCP parameter validation**: Missing required parameters return readable `Validation Error`, not raw `KeyError`
4. **MCP output truncation**: Long tool outputs auto-truncated with marker to prevent context explosion
5. **Bounded cache**: Sandbox cache has LRU eviction to prevent unbounded memory growth
