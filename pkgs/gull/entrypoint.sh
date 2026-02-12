#!/bin/bash
# Docker entrypoint script for Gull container
# Injects built-in skills into the shared workspace, then starts the application.

set -e

# Inject built-in skills into /workspace/skills/ (per-skill overwrite).
# Each skill directory in /app/skills/ is individually rm+cp'd so that:
#   - built-in skills are always at the image version (idempotent)
#   - skills from other runtimes (Ship) or upper-layer agents are untouched
if [ -d /app/skills ] && [ "$(ls -A /app/skills 2>/dev/null)" ]; then
    mkdir -p /workspace/skills
    for skill_dir in /app/skills/*/; do
        [ -d "$skill_dir" ] || continue
        skill_name=$(basename "$skill_dir")
        rm -rf "/workspace/skills/$skill_name"
        cp -r "$skill_dir" "/workspace/skills/$skill_name"
    done
    echo "[gull] injected built-in skills to /workspace/skills/"
fi

# Execute the main command
exec "$@"
