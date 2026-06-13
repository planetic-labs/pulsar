#!/usr/bin/env bash
# Script for Pulsar restore using Restic

set -eo pipefail

# Get script and project directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load environment variables
if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    source "$PROJECT_ROOT/.env"
    set +a
fi

# Validate configuration
if [ -z "$RESTIC_REPOSITORY" ] || [ -z "$RESTIC_PASSWORD" ]; then
    echo "❌ Error: RESTIC_REPOSITORY or RESTIC_PASSWORD is not set in .env" >&2
    exit 1
fi

if [ -z "$S3_ACCESS_KEY" ] || [ -z "$S3_SECRET_KEY" ]; then
    echo "❌ Error: S3_ACCESS_KEY or S3_SECRET_KEY is not set in .env" >&2
    exit 1
fi

# Map S3 configuration to AWS variables expected by Restic
export AWS_ACCESS_KEY_ID="$S3_ACCESS_KEY"
export AWS_SECRET_ACCESS_KEY="$S3_SECRET_KEY"
if [ -n "$S3_REGION_NAME" ]; then
    export AWS_DEFAULT_REGION="$S3_REGION_NAME"
fi

# Check if restic CLI is installed
if ! command -v restic &> /dev/null; then
    echo "❌ Error: restic is not installed." >&2
    exit 1
fi

echo "📋 List of available snapshots in repository:"
restic snapshots

echo ""
echo "Select snapshot to restore (default: latest):"
read -p "Snapshot ID: " SNAPSHOT_ID
SNAPSHOT_ID=${SNAPSHOT_ID:-latest}

RESTORE_DIR="$PROJECT_ROOT/tmp/restore_$SNAPSHOT_ID"
mkdir -p "$RESTORE_DIR"

echo "🔄 Restoring snapshot '$SNAPSHOT_ID' to temporary directory '$RESTORE_DIR'..."
restic restore "$SNAPSHOT_ID" --target "$RESTORE_DIR"

    echo "========================================="
    echo "✅ Restore completed to $RESTORE_DIR"
    echo "========================================="
    echo ""

    read -p "⚠️  Do you want to apply these restored files to your active Pulsar system now? (y/N): " CONFIRM
    if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
        # Detect docker compose version / command
        COMPOSE_CMD="docker compose"
        if ! command -v docker-compose &> /dev/null && command -v docker &> /dev/null; then
            if ! docker compose version &> /dev/null; then
                COMPOSE_CMD="docker-compose"
            fi
        fi

        echo "🛑 Stopping pulsar containers..."
        if $COMPOSE_CMD down &> /dev/null; then
            echo "✅ Containers stopped."
        else
            echo "⚠️  Could not stop containers via docker compose, proceeding with file copy..."
        fi

        echo "🔄 Copying storage files..."
        if [ -d "$RESTORE_DIR$PROJECT_ROOT/storage" ]; then
            cp -r "$RESTORE_DIR$PROJECT_ROOT/storage/"* "$PROJECT_ROOT/storage/"
        fi

        echo "🔄 Copying configuration files..."
        if [ -d "$RESTORE_DIR$PROJECT_ROOT/config" ]; then
            cp -r "$RESTORE_DIR$PROJECT_ROOT/config/"* "$PROJECT_ROOT/config/"
        fi
        if [ -f "$RESTORE_DIR$PROJECT_ROOT/.env" ]; then
            cp "$RESTORE_DIR$PROJECT_ROOT/.env" "$PROJECT_ROOT/.env"
        fi

        echo "🔄 Restoring SQLite database..."
        DB_RESTORED_PATH="$RESTORE_DIR$PROJECT_ROOT/tmp/backup_db/pulsar.db"

        if [ -f "$DB_RESTORED_PATH" ]; then
            mkdir -p "$PROJECT_ROOT/data"
            cp "$DB_RESTORED_PATH" "$PROJECT_ROOT/data/pulsar.db"
            echo "✅ Database restored."
        else
            echo "❌ Error: SQLite backup file not found in snapshot at $PROJECT_ROOT/tmp/backup_db/pulsar.db."
        fi

        echo "🧹 Cleaning up temporary restore directory..."
        rm -rf "$RESTORE_DIR"

    echo "⚡ Starting pulsar containers..."
    if $COMPOSE_CMD up -d; then
        echo "✅ Containers started successfully. RESTORE COMPLETE! 🎉"
    else
        echo "❌ Error: Failed to start containers. Please run 'docker compose up -d' manually."
    fi
else
    echo "ℹ️ Apply canceled. Restored files remain intact in $RESTORE_DIR"
fi

