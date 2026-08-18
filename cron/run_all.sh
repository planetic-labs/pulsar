#!/bin/bash
# Get the directory where the script is located, then resolve the project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

# Run the unified cron workflow using root environment dependencies and pass all arguments
if command -v uv &> /dev/null; then
    uv run --python 3.14 python cron/run_all.py "$@"
else
    if [ -d ".venv" ]; then
        source .venv/bin/activate
    fi
    python cron/run_all.py "$@"
fi
