#!/bin/bash
# setup-cursor.sh - Set up Cursor IDE skills, rules, and MCP configuration
# Run on fresh deployments to symlink hol/.cursor content into ~/.cursor
#
# This ensures the Cursor IDE picks up VCF skills, rules, and MCP servers
# from the git-tracked copies in /home/holuser/hol/.cursor/ rather than
# requiring manual setup on each new deployment.
#
# Called by: labstartup.sh (console section) or manually
# Idempotent: safe to run multiple times

HOL_CURSOR="/home/holuser/hol/.cursor"
USER_CURSOR="/home/holuser/.cursor"

if [ ! -d "$HOL_CURSOR" ]; then
    echo "setup-cursor: $HOL_CURSOR not found, skipping"
    exit 0
fi

mkdir -p "$USER_CURSOR"

for item in skills rules mcp.json; do
    src="$HOL_CURSOR/$item"
    dst="$USER_CURSOR/$item"

    if [ ! -e "$src" ]; then
        continue
    fi

    if [ -L "$dst" ]; then
        current_target=$(readlink -f "$dst")
        expected_target=$(readlink -f "$src")
        if [ "$current_target" = "$expected_target" ]; then
            continue
        fi
        rm -f "$dst"
    elif [ -e "$dst" ]; then
        rm -rf "$dst"
    fi

    ln -s "$src" "$dst"
    echo "setup-cursor: linked $dst -> $src"
done

echo "setup-cursor: done"
