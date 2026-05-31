#!/bin/bash
# Get the directory where the script is located, then resolve the project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

# Run backup script using the root environment dependencies
if command -v uv &> /dev/null; then
    uv run --python 3.12 python backups/backup.py
else
    if [ -d ".venv" ]; then
        source .venv/bin/activate
    fi
    python backups/backup.py
fi
