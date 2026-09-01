#!/usr/bin/env bash
# Script for Pulsar backup using Restic
# Recommended to run via cron daily at night.

set -eo pipefail

# Get script and project directories
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

# Load environment variables
if [ -f "$PROJECT_ROOT/.env" ]; then
    # Load env variables without exported shell formatting issues
    set -a
    source "$PROJECT_ROOT/.env"
    set +a
fi

# Ensure logs directory exists
LOGS_DIR="$PROJECT_ROOT/logs"
mkdir -p "$LOGS_DIR"
LOG_FILE="$LOGS_DIR/restic_backup.log"

# Setup logging to both file and stdout
exec > >(tee -a "$LOG_FILE") 2>&1

echo "========================================="
echo "🚀 STARTING RESTIC BACKUP AT $(date)"
echo "========================================="

# Validate essential configuration
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
    echo "❌ Error: restic is not installed. Please install it first." >&2
    exit 1
fi

# Check if sqlite3 CLI is installed
if ! command -v sqlite3 &> /dev/null; then
    echo "❌ Error: sqlite3 command line utility is not installed." >&2
    exit 1
fi

# Proactively initialize restic repository if it doesn't exist
if ! restic snapshots &> /dev/null; then
    echo "📦 Repository is not initialized. Initializing now..."
    restic init
    echo "✅ Repository initialized successfully."
fi

# Setup paths
TEMP_DB_DIR="$PROJECT_ROOT/tmp/backup_db"
TEMP_DB_PATH="$TEMP_DB_DIR/pulsar.db"
DB_SOURCE_PATH="$PROJECT_ROOT/data/pulsar.db"

cleanup_temp_db() {
    rm -f "$TEMP_DB_PATH"
    rmdir "$TEMP_DB_DIR" 2> /dev/null || true
}

trap cleanup_temp_db EXIT

# Create a safe consistent SQLite hot copy
echo "📦 Creating consistent copy of SQLite database..."
if [ ! -f "$DB_SOURCE_PATH" ]; then
    echo "❌ Error: Database file not found at $DB_SOURCE_PATH" >&2
    exit 1
fi

cleanup_temp_db
mkdir -p "$TEMP_DB_DIR"
if ! sqlite3 "$DB_SOURCE_PATH" ".backup '$TEMP_DB_PATH'"; then
    echo "❌ Error: Failed to create SQLite backup copy." >&2
    exit 1
fi

if ! INTEGRITY_RESULT="$(sqlite3 "$TEMP_DB_PATH" "PRAGMA integrity_check;")"; then
    echo "❌ Error: Failed to validate SQLite backup copy." >&2
    exit 1
fi

if [ "$INTEGRITY_RESULT" != "ok" ]; then
    echo "❌ Error: SQLite backup copy failed integrity check:" >&2
    printf '%s\n' "$INTEGRITY_RESULT" >&2
    exit 1
fi

echo "✅ SQLite backup copy created and verified at $TEMP_DB_PATH"

# Prepare backup targets
BACKUP_TARGETS=()
BACKUP_TARGETS+=("$PROJECT_ROOT/.env")

if [ -d "$PROJECT_ROOT/config" ]; then
    BACKUP_TARGETS+=("$PROJECT_ROOT/config")
fi

if [ -d "$PROJECT_ROOT/storage/transcripts" ]; then
    BACKUP_TARGETS+=("$PROJECT_ROOT/storage/transcripts")
fi

if [ -f "$TEMP_DB_PATH" ]; then
    BACKUP_TARGETS+=("$TEMP_DB_PATH")
fi

# Run restic backup
echo "☁️ Running restic backup to $RESTIC_REPOSITORY..."
if restic backup \
    --host "pulsar-host" \
    --tag "cron" \
    "${BACKUP_TARGETS[@]}"; then
    echo "✅ Restic backup finished successfully."
else
    echo "❌ Error: Restic backup failed." >&2
    exit 1
fi

# Clean up temporary DB copy
echo "🧹 Cleaning up temporary DB files..."
cleanup_temp_db

# Run forget & prune policy (Retention)
echo "🔄 Applying retention policy (forget & prune)..."
restic forget \
    --keep-daily "${RESTIC_KEEP_DAILY:-7}" \
    --keep-weekly "${RESTIC_KEEP_WEEKLY:-4}" \
    --keep-monthly "${RESTIC_KEEP_MONTHLY:-12}" \
    --prune

# Verify backup repository integrity (fast check)
echo "🔍 Running quick repository health check..."
restic check --read-data-subset=10%

echo "========================================="
echo "🎉 RESTIC BACKUP FINISHED SUCCESSFULLY AT $(date)"
echo "========================================="

# Send Telegram notification if configured
if [ -n "$TELEGRAM_BOT_TOKEN" ] && [ -n "$TELEGRAM_CHAT_ID" ]; then
    MSG="<b>✅ Резервное копирование Pulsar завершено успешно!</b>%0A📅 Дата: $(date)%0A💾 Репозиторий: <code>$RESTIC_REPOSITORY</code>"
    curl -s -X POST "https://api.telegram.org/bot$TELEGRAM_BOT_TOKEN/sendMessage" \
        -d "chat_id=$TELEGRAM_CHAT_ID" \
        -d "text=$MSG" \
        -d "parse_mode=HTML" > /dev/null
fi
