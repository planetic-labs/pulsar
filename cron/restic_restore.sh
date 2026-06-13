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

RESTORE_DIR="/tmp/pulsar_restore_$SNAPSHOT_ID"
mkdir -p "$RESTORE_DIR"

echo "🔄 Restoring snapshot '$SNAPSHOT_ID' to temporary directory '$RESTORE_DIR'..."
restic restore "$SNAPSHOT_ID" --target "$RESTORE_DIR"

echo "========================================="
echo "✅ Restore completed to $RESTORE_DIR"
echo "========================================="
echo ""
echo "⚠️  CRITICAL: Before applying, stop the application container first!"
echo "   Command: docker compose down"
echo ""
echo "To copy restored files back to original project folders, run these commands:"
echo "------------------------------------------------------------------------"
echo "# Copy storage files back"
echo "cp -r \"$RESTORE_DIR$PROJECT_ROOT/storage/\"* \"$PROJECT_ROOT/storage/\""
echo ""
echo "# Copy configuration back"
echo "cp -r \"$RESTORE_DIR$PROJECT_ROOT/config/\"* \"$PROJECT_ROOT/config/\""
echo "cp \"$RESTORE_DIR$PROJECT_ROOT/.env\" \"$PROJECT_ROOT/.env\""
echo ""
echo "# Restore SQLite database file"
if [ -d "$RESTORE_DIR/tmp/pulsar_backup_db" ]; then
    echo "cp \"$RESTORE_DIR/tmp/pulsar_backup_db/pulsar.db\" \"$PROJECT_ROOT/data/pulsar.db\""
else
    echo "# Note: SQLite backup not found in /tmp path of snapshot. Restore manually from restored files."
fi
echo "------------------------------------------------------------------------"
