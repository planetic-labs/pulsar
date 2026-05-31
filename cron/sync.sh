#!/bin/bash
# Get the directory where the script is located, then resolve the project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

cd "$PROJECT_ROOT"

# Run the synchronization script inside the running app container
# We use -T flag to disable pseudo-TTY allocation since it's running in cron
if command -v docker-compose &> /dev/null; then
    docker-compose exec -T pulsar uv run python scripts/sync_index.py
else
    docker compose exec -T pulsar uv run python scripts/sync_index.py
fi
