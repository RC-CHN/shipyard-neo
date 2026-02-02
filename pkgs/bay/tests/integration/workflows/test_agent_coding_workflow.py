"""AI Agent Code Generation & Iterative Fix workflow tests.

Purpose: Simulate an LLM-based coding agent's workflow:
- Create sandbox with TTL and idempotency protection
- Generate code that may fail initially
- Parse traceback and fix errors iteratively
- Extend TTL when task takes longer than expected
- Verify variable sharing across execution rounds

See: plans/phase-1/e2e-workflow-scenarios.md - Scenario 6

Note: workflow 场景测试默认会被 SERIAL_GROUPS["workflows"] 归类为 serial/workflows，
在“两阶段”执行流程的 Phase 2 独占 Bay 跑。
"""

from __future__ import annotations

import uuid

import httpx

from ..conftest import AUTH_HEADERS, BAY_BASE_URL, DEFAULT_PROFILE, e2e_skipif_marks

pytestmark = e2e_skipif_marks


class TestAgentCodingWorkflow:
    """AI Agent Code Generation and Iterative Fix (Agentic Coding workflow)."""

    async def test_agent_full_workflow_with_error_fix_and_extend_ttl(self):
        """Complete agent workflow: create -> generate buggy code -> fix -> extend TTL -> optimize.

        This simulates the full agent coding workflow:
        1. Create sandbox with idempotency key
        2. Write code with a typo bug
        3. Execute and get error (NameError)
        4. Parse error and fix the bug
        5. Execute fixed code successfully
        6. Extend TTL for more work
        7. Verify idempotent retry of extend_ttl
        8. Optimize code (use lru_cache)
        9. Verify variable sharing (use function from previous exec)
        10. Download final result
        11. Cleanup
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            task_id = f"agent-task-{uuid.uuid4().hex[:8]}"

            # Step 1: Create sandbox with TTL and idempotency key
            create_key = f"{task_id}-create"
            create_resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "ttl": 300},
                headers={"Idempotency-Key": create_key},
            )
            assert create_resp.status_code == 201
            sandbox = create_resp.json()
            sandbox_id = sandbox["id"]
            initial_expires_at = sandbox["expires_at"]
            assert initial_expires_at is not None

            try:
                # Step 2: Agent generates code with a bug (typo in function name)
                buggy_code = """def calculate_fibonacci(n):
    if n <= 1:
        return n
    return calculate_fibonacci(n-1) + calculate_fibonaci(n-2)  # typo: fibonaci

print(calculate_fibonacci(10))
"""
                write_resp = await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "solution.py", "content": buggy_code},
                    timeout=120.0,
                )
                assert write_resp.status_code == 200

                # Step 3: Execute buggy code - should fail with NameError
                exec1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "exec(open('solution.py').read())", "timeout": 30},
                    timeout=30.0,
                )
                assert exec1.status_code == 200
                result1 = exec1.json()
                assert result1["success"] is False, "Buggy code should fail"
                assert "NameError" in (result1.get("error") or ""), (
                    f"Expected NameError for typo, got: {result1}"
                )
                assert "calculate_fibonaci" in (result1.get("error") or ""), (
                    "Error should mention the typo 'calculate_fibonaci'"
                )

                # Step 4: Agent parses error and fixes the bug
                fixed_code = """def calculate_fibonacci(n):
    if n <= 1:
        return n
    return calculate_fibonacci(n-1) + calculate_fibonacci(n-2)

print(calculate_fibonacci(10))
"""
                write_resp2 = await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "solution.py", "content": fixed_code},
                    timeout=30.0,
                )
                assert write_resp2.status_code == 200

                # Step 5: Execute fixed code - should succeed
                exec2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "exec(open('solution.py').read())", "timeout": 30},
                    timeout=30.0,
                )
                assert exec2.status_code == 200
                result2 = exec2.json()
                assert result2["success"] is True, f"Fixed code should succeed: {result2}"
                assert "55" in result2["output"], (
                    f"Expected fibonacci(10) = 55, got: {result2['output']}"
                )

                # Step 6: Agent needs more time, extend TTL
                extend_key = f"{task_id}-extend-1"
                extend_resp = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                    json={"extend_by": 600},
                    headers={"Idempotency-Key": extend_key},
                )
                assert extend_resp.status_code == 200
                extended = extend_resp.json()
                new_expires_at = extended["expires_at"]
                assert new_expires_at != initial_expires_at, "expires_at should be updated"

                # Step 7: Simulate network retry - same idempotency key
                extend_resp2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/extend_ttl",
                    json={"extend_by": 600},
                    headers={"Idempotency-Key": extend_key},
                )
                assert extend_resp2.status_code == 200
                assert extend_resp2.json()["expires_at"] == new_expires_at, (
                    "Idempotent retry should return same expires_at"
                )

                # Step 8: Agent optimizes code with lru_cache
                optimized_code = """from functools import lru_cache

@lru_cache(maxsize=None)
def calculate_fibonacci(n):
    if n <= 1:
        return n
    return calculate_fibonacci(n-1) + calculate_fibonacci(n-2)

# Test with larger numbers
for i in [10, 20, 30]:
    print(f"fib({i}) = {calculate_fibonacci(i)}")
"""
                write_resp3 = await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "solution.py", "content": optimized_code},
                    timeout=30.0,
                )
                assert write_resp3.status_code == 200

                # Step 9: Execute optimized code
                exec3 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "exec(open('solution.py').read())", "timeout": 30},
                    timeout=30.0,
                )
                assert exec3.status_code == 200
                result3 = exec3.json()
                assert result3["success"] is True, f"Optimized code should succeed: {result3}"
                assert "fib(10) = 55" in result3["output"]
                assert "fib(20) = 6765" in result3["output"]
                assert "fib(30) = 832040" in result3["output"]

                # Step 10: Verify variable sharing - use function from previous exec
                exec4 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={
                        "code": "print(f'fib(40) = {calculate_fibonacci(40)}')",
                        "timeout": 30,
                    },
                    timeout=30.0,
                )
                assert exec4.status_code == 200
                result4 = exec4.json()
                assert result4["success"] is True, (
                    f"Should use function from previous exec: {result4}"
                )
                assert "fib(40) = 102334155" in result4["output"]

                # Step 11: Download final code
                read_resp = await client.get(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    params={"path": "solution.py"},
                    timeout=30.0,
                )
                assert read_resp.status_code == 200
                final_code = read_resp.json()["content"]
                assert "lru_cache" in final_code, "Final code should contain optimization"

            finally:
                # Cleanup
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_agent_error_provides_useful_traceback(self):
        """Error response should contain traceback useful for agent to parse and fix.

        Tests:
        - TypeError with clear message
        - IndexError with clear message
        - AttributeError with clear message
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_resp.status_code == 201
            sandbox_id = create_resp.json()["id"]

            try:
                # Test 1: TypeError
                exec1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "'hello' + 42", "timeout": 30},
                    timeout=120.0,
                )
                assert exec1.status_code == 200
                result1 = exec1.json()
                assert result1["success"] is False
                assert "TypeError" in (result1.get("error") or "")

                # Test 2: IndexError
                exec2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "lst = [1, 2, 3]; print(lst[10])", "timeout": 30},
                    timeout=30.0,
                )
                assert exec2.status_code == 200
                result2 = exec2.json()
                assert result2["success"] is False
                assert "IndexError" in (result2.get("error") or "")

                # Test 3: AttributeError
                exec3 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "x = 42; x.append(1)", "timeout": 30},
                    timeout=30.0,
                )
                assert exec3.status_code == 200
                result3 = exec3.json()
                assert result3["success"] is False
                assert "AttributeError" in (result3.get("error") or "")

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_agent_create_idempotency(self):
        """Agent can safely retry sandbox creation with same idempotency key.

        Tests:
        - Same key returns same sandbox
        - Different key creates different sandbox
        """
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            task_id = f"agent-{uuid.uuid4().hex[:8]}"
            create_key = f"{task_id}-create"

            # First create
            create1 = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE, "ttl": 300},
                headers={"Idempotency-Key": create_key},
            )
            assert create1.status_code == 201
            sandbox1 = create1.json()
            sandbox_id1 = sandbox1["id"]

            try:
                # Retry with same key - should get same sandbox
                create2 = await client.post(
                    "/v1/sandboxes",
                    json={"profile": DEFAULT_PROFILE, "ttl": 300},
                    headers={"Idempotency-Key": create_key},
                )
                assert create2.status_code == 201
                sandbox2 = create2.json()
                assert sandbox2["id"] == sandbox_id1, (
                    "Same idempotency key should return same sandbox"
                )

                # Different key - should create new sandbox
                create3 = await client.post(
                    "/v1/sandboxes",
                    json={"profile": DEFAULT_PROFILE, "ttl": 300},
                    headers={"Idempotency-Key": f"{task_id}-create-2"},
                )
                assert create3.status_code == 201
                sandbox3 = create3.json()
                sandbox_id3 = sandbox3["id"]
                assert sandbox_id3 != sandbox_id1, (
                    "Different idempotency key should create new sandbox"
                )

                # Cleanup second sandbox
                await client.delete(f"/v1/sandboxes/{sandbox_id3}")

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id1}")

    async def test_agent_iterative_fix_with_multiple_files(self):
        """Agent can work with multiple files and fix errors across files."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_resp.status_code == 201
            sandbox_id = create_resp.json()["id"]

            try:
                buggy_utils = """def add(a, b):
    result = a + b
    # Missing return statement!

def multiply(a, b):
    return a * b
"""
                await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "utils.py", "content": buggy_utils},
                    timeout=120.0,
                )

                main_code = """from utils import add, multiply

result = add(2, 3)
print(f"add(2, 3) = {result}")
print(f"multiply(2, 3) = {multiply(2, 3)}")
"""
                await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "main.py", "content": main_code},
                    timeout=30.0,
                )

                exec1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={"code": "exec(open('main.py').read())", "timeout": 30},
                    timeout=30.0,
                )
                assert exec1.status_code == 200
                result1 = exec1.json()
                assert result1["success"] is True
                assert "None" in result1["output"], "Buggy add() should return None"

                fixed_utils = """def add(a, b):
    result = a + b
    return result

def multiply(a, b):
    return a * b
"""
                await client.put(
                    f"/v1/sandboxes/{sandbox_id}/filesystem/files",
                    json={"path": "utils.py", "content": fixed_utils},
                    timeout=30.0,
                )

                exec2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={
                        "code": """
import importlib
import utils
importlib.reload(utils)
from utils import add, multiply
print(f"add(2, 3) = {add(2, 3)}")
print(f"multiply(2, 3) = {multiply(2, 3)}")
""",
                        "timeout": 30,
                    },
                    timeout=30.0,
                )
                assert exec2.status_code == 200
                result2 = exec2.json()
                assert result2["success"] is True
                assert "add(2, 3) = 5" in result2["output"]
                assert "multiply(2, 3) = 6" in result2["output"]

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")

    async def test_agent_variable_persistence_within_session(self):
        """Variables defined in one exec call persist in subsequent calls within same session."""
        async with httpx.AsyncClient(base_url=BAY_BASE_URL, headers=AUTH_HEADERS) as client:
            create_resp = await client.post(
                "/v1/sandboxes",
                json={"profile": DEFAULT_PROFILE},
            )
            assert create_resp.status_code == 201
            sandbox_id = create_resp.json()["id"]

            try:
                exec1 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={
                        "code": """
def process_data(items):
    return [x * 2 for x in items]

results = []
print("Helper function defined")
""",
                        "timeout": 30,
                    },
                    timeout=120.0,
                )
                assert exec1.status_code == 200
                assert exec1.json()["success"] is True

                exec2 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={
                        "code": """
batch1 = process_data([1, 2, 3])
results.extend(batch1)
print(f"Batch 1: {batch1}")
""",
                        "timeout": 30,
                    },
                    timeout=30.0,
                )
                assert exec2.status_code == 200
                result2 = exec2.json()
                assert result2["success"] is True
                assert "Batch 1: [2, 4, 6]" in result2["output"]

                exec3 = await client.post(
                    f"/v1/sandboxes/{sandbox_id}/python/exec",
                    json={
                        "code": """
batch2 = process_data([4, 5, 6])
results.extend(batch2)
print(f"Batch 2: {batch2}")
print(f"All results: {results}")
""",
                        "timeout": 30,
                    },
                    timeout=30.0,
                )
                assert exec3.status_code == 200
                result3 = exec3.json()
                assert result3["success"] is True
                assert "Batch 2: [8, 10, 12]" in result3["output"]
                assert "All results: [2, 4, 6, 8, 10, 12]" in result3["output"]

            finally:
                await client.delete(f"/v1/sandboxes/{sandbox_id}")
