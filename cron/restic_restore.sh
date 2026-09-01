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

if ! command -v sqlite3 &> /dev/null; then
    echo "❌ Error: sqlite3 is required for post-restore validation." >&2
    exit 1
fi

echo "📋 List of available snapshots in repository:"
restic snapshots

echo ""
echo "Select snapshot to restore (default: latest):"
read -p "Snapshot ID: " SNAPSHOT_ID
SNAPSHOT_ID=${SNAPSHOT_ID:-latest}

RESTORE_DIR="$PROJECT_ROOT/tmp/restore_$SNAPSHOT_ID"
if [ -e "$RESTORE_DIR" ]; then
    echo "❌ Error: restore directory already exists: $RESTORE_DIR" >&2
    exit 1
fi
mkdir -p "$RESTORE_DIR"

echo "🔄 Restoring snapshot '$SNAPSHOT_ID' to temporary directory '$RESTORE_DIR'..."
restic restore "$SNAPSHOT_ID" --target "$RESTORE_DIR"

echo "========================================="
echo "✅ Restore completed to $RESTORE_DIR"
echo "========================================="
echo ""

DB_RESTORED_PATH="$RESTORE_DIR$PROJECT_ROOT/tmp/backup_db/pulsar.db"
MANIFEST_RESTORED_PATH="$RESTORE_DIR$PROJECT_ROOT/tmp/backup_db/manifest.json"
MANTICORE_RESTORED_DIR="$RESTORE_DIR$PROJECT_ROOT/tmp/backup_manticore"
TARGET_ENVIRONMENT="${PULSAR_ENV:-dev}"
if [[ "${COMPOSE_FILE:-}" == *"prod"* ]]; then
    TARGET_ENVIRONMENT="prod"
fi

if [ ! -f "$DB_RESTORED_PATH" ] || [ ! -f "$MANIFEST_RESTORED_PATH" ] || [ ! -d "$MANTICORE_RESTORED_DIR" ]; then
    echo "❌ Error: snapshot is incomplete; SQLite, Manticore, and manifest are all required." >&2
    echo "   Database: $DB_RESTORED_PATH" >&2
    echo "   Manifest: $MANIFEST_RESTORED_PATH" >&2
    echo "   Manticore: $MANTICORE_RESTORED_DIR" >&2
    exit 1
fi

echo "🔍 Validating checksum, schema, integrity, row counts, and target environment..."
python3 "$PROJECT_ROOT/scripts/backup_manifest.py" validate \
    --db "$DB_RESTORED_PATH" \
    --manifest "$MANIFEST_RESTORED_PATH" \
    --manticore-backup "$MANTICORE_RESTORED_DIR" \
    --environment "$TARGET_ENVIRONMENT" > /dev/null
echo "✅ Snapshot preflight passed. No active files have been changed."

read -p "⚠️  Do you want to apply these restored files to your active Pulsar system now? (y/N): " CONFIRM
if [[ "$CONFIRM" =~ ^[Yy]$ ]]; then
    # Detect docker compose version / command
    if docker compose version &> /dev/null; then
        COMPOSE_CMD=(docker compose)
    elif command -v docker-compose &> /dev/null; then
        COMPOSE_CMD=(docker-compose)
    else
        echo "❌ Docker Compose is required for Manticore restore." >&2
        exit 1
    fi

    echo "🛑 Stopping pulsar containers..."
    if "${COMPOSE_CMD[@]}" down &> /dev/null; then
        echo "✅ Containers stopped."
    else
        echo "❌ Could not stop containers; refusing to change active files." >&2
        exit 1
    fi

    MANTICORE_BACKUP_NAME="$(find "$MANTICORE_RESTORED_DIR" -mindepth 1 -maxdepth 1 -type d -name 'backup-*' -printf '%f\n')"
    if [ -z "$MANTICORE_BACKUP_NAME" ] || [ "$(printf '%s\n' "$MANTICORE_BACKUP_NAME" | wc -l)" -ne 1 ]; then
        echo "❌ Expected exactly one Manticore backup directory." >&2
        exit 1
    fi

    echo "🔄 Restoring the physical Manticore backup '$MANTICORE_BACKUP_NAME'..."
    "${COMPOSE_CMD[@]}" run --rm --no-deps \
        -v "$MANTICORE_RESTORED_DIR:/backup:ro" \
        --entrypoint sh manticore \
        -c "find /var/lib/manticore -mindepth 1 -delete && manticore-backup --backup-dir=/backup --restore=$MANTICORE_BACKUP_NAME --disable-telemetry"
    echo "✅ Manticore database restored."

    echo "🔄 Copying storage files..."
    if [ -d "$RESTORE_DIR$PROJECT_ROOT/storage" ]; then
        cp -r "$RESTORE_DIR$PROJECT_ROOT/storage/"* "$PROJECT_ROOT/storage/"
    fi

    echo "🔄 Copying non-secret configuration files..."
    if [ -d "$RESTORE_DIR$PROJECT_ROOT/config" ]; then
        cp -r "$RESTORE_DIR$PROJECT_ROOT/config/"* "$PROJECT_ROOT/config/"
    fi
    if [ -f "$RESTORE_DIR$PROJECT_ROOT/.env" ]; then
        cp "$RESTORE_DIR$PROJECT_ROOT/.env" "$PROJECT_ROOT/.env.restored"
        echo "ℹ️ Restored .env saved as .env.restored for review; active .env was not overwritten."
    fi

    echo "🔄 Restoring SQLite database..."
    mkdir -p "$PROJECT_ROOT/data"
    cp "$DB_RESTORED_PATH" "$PROJECT_ROOT/data/pulsar.db.restore-new"
    mv "$PROJECT_ROOT/data/pulsar.db.restore-new" "$PROJECT_ROOT/data/pulsar.db"
    cp "$MANIFEST_RESTORED_PATH" "$PROJECT_ROOT/data/restore-manifest.json"
    rm -f "$PROJECT_ROOT/data/REINDEX_REQUIRED"
    echo "✅ SQLite restored atomically; it is paired with the restored Manticore snapshot."

    echo "🧹 Cleaning up temporary restore directory..."
    rm -rf "$RESTORE_DIR"

    echo "⚡ Starting pulsar containers..."
    if "${COMPOSE_CMD[@]}" up -d; then
        RESTORED_SQLITE_COUNT="$(sqlite3 "$PROJECT_ROOT/data/pulsar.db" "SELECT COUNT(*) FROM chunks;")"
        RESTORED_MANTICORE_OUTPUT="$("${COMPOSE_CMD[@]}" exec -T manticore mysql -h127.0.0.1 -P9306 -e 'SELECT COUNT(*) FROM chunks')"
        RESTORED_MANTICORE_COUNT="$(printf '%s\n' "$RESTORED_MANTICORE_OUTPUT" | grep -Eo '[0-9]+' | tail -n 1)"
        if [ -z "$RESTORED_MANTICORE_COUNT" ] || [ "$RESTORED_SQLITE_COUNT" -ne "$RESTORED_MANTICORE_COUNT" ]; then
            echo "❌ Restore count validation failed: SQLite=$RESTORED_SQLITE_COUNT, Manticore=${RESTORED_MANTICORE_COUNT:-unknown}." >&2
            exit 1
        fi
        echo "✅ Containers started; SQLite/Manticore counts match ($RESTORED_SQLITE_COUNT). RESTORE COMPLETE! 🎉"
    else
        echo "❌ Error: Failed to start containers. Please run 'docker compose up -d' manually."
    fi
else
    echo "ℹ️ Apply canceled. Restored files remain intact in $RESTORE_DIR"
fi
