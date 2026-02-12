#!/bin/bash
# Docker entrypoint script for Ship container
# This script runs as root to fix workspace permissions,
# injects built-in skills into the shared workspace, then starts the application.

set -e

# Fix workspace permissions for shipyard user
# This is needed when /workspace is a mounted volume with root ownership
if [ -d /workspace ]; then
    chown -R shipyard:shipyard /workspace 2>/dev/null || true
    chmod 755 /workspace 2>/dev/null || true
fi

# Inject built-in skills into /workspace/skills/ (per-skill overwrite).
# Each skill directory in /app/skills/ is individually rm+cp'd so that:
#   - built-in skills are always at the image version (idempotent)
#   - skills from other runtimes (Gull) or upper-layer agents are untouched
if [ -d /app/skills ] && [ "$(ls -A /app/skills 2>/dev/null)" ]; then
    mkdir -p /workspace/skills
    for skill_dir in /app/skills/*/; do
        [ -d "$skill_dir" ] || continue
        skill_name=$(basename "$skill_dir")
        rm -rf "/workspace/skills/$skill_name"
        cp -r "$skill_dir" "/workspace/skills/$skill_name"
    done
    chown -R shipyard:shipyard /workspace/skills
    echo "[ship] injected built-in skills to /workspace/skills/"
fi

# Execute the main command
exec "$@"
