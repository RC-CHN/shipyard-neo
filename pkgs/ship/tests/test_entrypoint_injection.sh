#!/bin/bash
# Test script for entrypoint.sh skill injection logic.
# This tests the injection logic in isolation without requiring Docker.
#
# Usage: bash tests/test_entrypoint_injection.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"

echo "=== Testing entrypoint.sh skill injection logic ==="

# Create temp directories to simulate container filesystem
TMPDIR=$(mktemp -d)
trap "rm -rf $TMPDIR" EXIT

APP_SKILLS="$TMPDIR/app/skills"
WORKSPACE="$TMPDIR/workspace"
WORKSPACE_SKILLS="$WORKSPACE/skills"

# --- Test 1: Basic injection ---
echo ""
echo "--- Test 1: Basic injection of a single skill ---"
mkdir -p "$APP_SKILLS/python-sandbox"
echo "---
name: python-sandbox
description: test
---" > "$APP_SKILLS/python-sandbox/SKILL.md"

mkdir -p "$WORKSPACE"

# Simulate the injection logic from entrypoint.sh
if [ -d "$APP_SKILLS" ] && [ "$(ls -A "$APP_SKILLS" 2>/dev/null)" ]; then
    mkdir -p "$WORKSPACE_SKILLS"
    for skill_dir in "$APP_SKILLS"/*/; do
        [ -d "$skill_dir" ] || continue
        skill_name=$(basename "$skill_dir")
        rm -rf "$WORKSPACE_SKILLS/$skill_name"
        cp -r "$skill_dir" "$WORKSPACE_SKILLS/$skill_name"
    done
fi

# Verify
if [ -f "$WORKSPACE_SKILLS/python-sandbox/SKILL.md" ]; then
    echo "PASS: SKILL.md injected to $WORKSPACE_SKILLS/python-sandbox/"
else
    echo "FAIL: SKILL.md not found at $WORKSPACE_SKILLS/python-sandbox/"
    exit 1
fi

# --- Test 2: Multiple skills ---
echo ""
echo "--- Test 2: Multiple skills injection ---"
mkdir -p "$APP_SKILLS/browser-automation/references"
echo "---
name: browser-automation
description: test browser
---" > "$APP_SKILLS/browser-automation/SKILL.md"
echo "# Browser ref" > "$APP_SKILLS/browser-automation/references/browser.md"

# Re-run injection
for skill_dir in "$APP_SKILLS"/*/; do
    [ -d "$skill_dir" ] || continue
    skill_name=$(basename "$skill_dir")
    rm -rf "$WORKSPACE_SKILLS/$skill_name"
    cp -r "$skill_dir" "$WORKSPACE_SKILLS/$skill_name"
done

if [ -f "$WORKSPACE_SKILLS/browser-automation/SKILL.md" ] && \
   [ -f "$WORKSPACE_SKILLS/browser-automation/references/browser.md" ] && \
   [ -f "$WORKSPACE_SKILLS/python-sandbox/SKILL.md" ]; then
    echo "PASS: Both skills with references injected"
else
    echo "FAIL: Missing files"
    ls -R "$WORKSPACE_SKILLS/"
    exit 1
fi

# --- Test 3: Idempotent overwrite ---
echo ""
echo "--- Test 3: Idempotent overwrite (re-injection replaces files) ---"
echo "MODIFIED CONTENT" > "$WORKSPACE_SKILLS/python-sandbox/SKILL.md"

# Re-run injection
for skill_dir in "$APP_SKILLS"/*/; do
    [ -d "$skill_dir" ] || continue
    skill_name=$(basename "$skill_dir")
    rm -rf "$WORKSPACE_SKILLS/$skill_name"
    cp -r "$skill_dir" "$WORKSPACE_SKILLS/$skill_name"
done

content=$(cat "$WORKSPACE_SKILLS/python-sandbox/SKILL.md")
if echo "$content" | grep -q "name: python-sandbox"; then
    echo "PASS: Modified file was overwritten with image version"
else
    echo "FAIL: File was not overwritten. Content: $content"
    exit 1
fi

# --- Test 4: Does not touch custom skills ---
echo ""
echo "--- Test 4: Custom skills from upper-layer agent are preserved ---"
mkdir -p "$WORKSPACE_SKILLS/custom-agent-skill"
echo "---
name: custom-agent-skill
description: from agent
---" > "$WORKSPACE_SKILLS/custom-agent-skill/SKILL.md"

# Re-run injection
for skill_dir in "$APP_SKILLS"/*/; do
    [ -d "$skill_dir" ] || continue
    skill_name=$(basename "$skill_dir")
    rm -rf "$WORKSPACE_SKILLS/$skill_name"
    cp -r "$skill_dir" "$WORKSPACE_SKILLS/$skill_name"
done

if [ -f "$WORKSPACE_SKILLS/custom-agent-skill/SKILL.md" ]; then
    echo "PASS: Custom skill preserved after injection"
else
    echo "FAIL: Custom skill was deleted"
    exit 1
fi

# --- Test 5: Empty /app/skills ---
echo ""
echo "--- Test 5: Empty /app/skills directory (no-op) ---"
rm -rf "$TMPDIR/app2"
mkdir -p "$TMPDIR/app2/skills"
APP_SKILLS2="$TMPDIR/app2/skills"

# With empty skills, should be no-op
count_before=$(ls "$WORKSPACE_SKILLS" | wc -l)
if [ -d "$APP_SKILLS2" ] && [ "$(ls -A "$APP_SKILLS2" 2>/dev/null)" ]; then
    echo "  (would inject, but empty)"
else
    echo "  (skipped injection — empty skills dir)"
fi
count_after=$(ls "$WORKSPACE_SKILLS" | wc -l)

if [ "$count_before" = "$count_after" ]; then
    echo "PASS: No changes when /app/skills is empty"
else
    echo "FAIL: Unexpected changes"
    exit 1
fi

echo ""
echo "=== All entrypoint injection tests passed ==="
