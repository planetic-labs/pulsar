import argparse
import logging
import os
import shutil
import subprocess
import sys
import tarfile
from pathlib import Path

import boto3
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# Setup clean logging
PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOGS_DIR = PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(exist_ok=True)
log_file = LOGS_DIR / "restore.log"

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
for h in root_logger.handlers[:]:
    root_logger.removeHandler(h)

# File handler
file_handler = logging.FileHandler(log_file, encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
root_logger.addHandler(file_handler)

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter("%(message)s"))
root_logger.addHandler(console_handler)

logger = logging.getLogger("restore")

# Load root .env file
ENV_PATH = PROJECT_ROOT / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    logger.warning(f"Root .env file not found in {PROJECT_ROOT}. Using system environment variables.")


# Helper to resolve container paths when running on host
def resolve_path(env_var: str, default_relative: str) -> Path:
    val = os.getenv(env_var)
    if val:
        p = Path(val)
        if p.exists():
            return p.resolve()
        # If path looks like container /app but we are on host
        if str(p).startswith("/app/"):
            relative_part = str(p)[5:]
            host_path = PROJECT_ROOT / relative_part
            if host_path.exists():
                return host_path.resolve()
        # Fallback to relative path if not absolute or doesn't exist
        host_path = PROJECT_ROOT / p.relative_to(p.anchor)
        if host_path.exists():
            return host_path.resolve()
    return (PROJECT_ROOT / default_relative).resolve()


DATA_DIR = resolve_path("APP_DATA_DIR", "data")
STORAGE_DIR = resolve_path("APP_STORAGE_DIR", "storage")

# Configuration
BACKUP_TOOL_DIR = PROJECT_ROOT / "backups"
LOCAL_BACKUP_DIR = BACKUP_TOOL_DIR / "local"
TEMP_RESTORE_DIR = BACKUP_TOOL_DIR / "tmp"

LOCAL_BACKUP_DIR.mkdir(exist_ok=True)
TEMP_RESTORE_DIR.mkdir(exist_ok=True)

# App Config
db_val = os.getenv("SQLITE_DB_PATH")
if db_val:
    p = Path(db_val)
    if p.exists():
        DB_PATH = p.resolve()
    elif str(p).startswith("/app/"):
        DB_PATH = (PROJECT_ROOT / str(p)[5:]).resolve()
    else:
        DB_PATH = (PROJECT_ROOT / p.relative_to(p.anchor)).resolve()
else:
    DB_PATH = DATA_DIR / "pulsar.db"

# Detect if running inside Docker container
is_container = Path("/.dockerenv").exists() or str(PROJECT_ROOT) == "/app"

# S3 Config
S3_ENDPOINT = os.getenv("S3_ENDPOINT_URL")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY")
S3_BUCKET = os.getenv("S3_BUCKET_NAME")
S3_REGION = os.getenv("S3_REGION_NAME", "us-east-1")


def get_s3_client():
    if not all([S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET]):
        return None
    return boto3.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
    )


def check_disk_space(archive_path: Path):
    """Check if there is enough space (5x archive size)."""
    if not archive_path.exists():
        return True

    archive_size = archive_path.stat().st_size
    required_space = archive_size * 5

    usage = shutil.disk_usage(BACKUP_TOOL_DIR)
    if usage.free < required_space:
        req_mb = required_space / 1024 / 1024
        free_mb = usage.free / 1024 / 1024
        logger.error(f"❌ Not enough disk space! Required: {req_mb:.2f} MB, Free: {free_mb:.2f} MB")
        return False
    return True


def manage_app_container(action: str):
    """Start or stop the pulsar container using docker compose."""
    compose_file = os.getenv("COMPOSE_FILE", "docker-compose.yml")
    cmd = ["docker", "compose", "-f", compose_file, action, "pulsar"]
    logger.info(f"🐳 Docker: {action}ing pulsar container using {compose_file}...")
    try:
        subprocess.run(cmd, cwd=PROJECT_ROOT, check=True, capture_output=True)
        logger.info(f"✅ Pulsar container {action}ed successfully.")
    except subprocess.CalledProcessError as e:
        logger.warning(f"⚠️ Failed to {action} pulsar container: {e.stderr.decode().strip()}")


def list_backups():
    logger.info("🔍 Available local backups (in local/):")
    local_backups = sorted(LOCAL_BACKUP_DIR.glob("*.tar.gz"), reverse=True)
    if local_backups:
        for i, b in enumerate(local_backups):
            p = Path(b)
            logger.info(f"  [{i}] {p.name} (Local)")
    else:
        logger.info("  (No local backups found)")

    s3 = get_s3_client()
    s3_backups = []
    if s3:
        logger.info("\n☁️ Available S3 backups:")
        try:
            res = s3.list_objects_v2(Bucket=S3_BUCKET)
            if "Contents" in res:
                s3_backups = sorted(
                    [obj["Key"] for obj in res["Contents"] if obj["Key"].endswith(".tar.gz")], reverse=True
                )
                for i, b in enumerate(s3_backups):
                    logger.info(f"  [S{i}] {b} (S3)")
            else:
                logger.info("  (No S3 backups found)")
        except ClientError as e:
            logger.error(f"Failed to list S3 backups: {e}")
    return local_backups, s3_backups


def download_with_progress(s3_client, bucket, key, dest_path):
    """Download from S3 with a simple progress indicator."""
    try:
        response = s3_client.head_object(Bucket=bucket, Key=key)
        total_size = float(response["ContentLength"])
    except Exception:
        total_size = None

    downloaded = 0

    def progress(bytes_amount):
        nonlocal downloaded
        downloaded += bytes_amount
        if total_size:
            percent = (downloaded / total_size) * 100
            sys.stdout.write(f"\r⬇️ Downloading: {percent:.1f}% ({downloaded / 1024 / 1024:.1f} MB)")
            sys.stdout.flush()

    logger.info(f"⬇️ Downloading {key} from S3 to local/...")
    s3_client.download_file(bucket, key, str(dest_path), Callback=progress)
    sys.stdout.write("\n")
    logger.info("✅ Download complete.")


def download_from_s3(key: str):
    dest_path = LOCAL_BACKUP_DIR / key
    if dest_path.exists():
        logger.info(f"Using already downloaded file: {dest_path}")
        return dest_path

    s3 = get_s3_client()
    if not s3:
        raise RuntimeError("S3 client not configured")

    LOCAL_BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    download_with_progress(s3, S3_BUCKET, key, dest_path)
    return dest_path


def main():
    parser = argparse.ArgumentParser(description="Restore Pulsar from backup")
    parser.add_argument("--file", help="Path to backup file (local or S3 key)")
    args = parser.parse_args()

    if not TEMP_RESTORE_DIR.exists():
        TEMP_RESTORE_DIR.mkdir(parents=True)

    local_backups, s3_backups = list_backups()
    freshest_local = local_backups[0] if local_backups else None
    freshest_s3 = s3_backups[0] if s3_backups else None

    default_choice = None
    suggestion = "None"

    if freshest_local and freshest_s3:
        p_local = Path(freshest_local)
        if p_local.name >= freshest_s3:
            default_choice = "0"
            suggestion = f"{p_local.name} (Local)"
        else:
            default_choice = "S0"
            suggestion = f"{freshest_s3} (S3 - will download to local/)"
    elif freshest_local:
        p_local = Path(freshest_local)
        default_choice = "0"
        suggestion = f"{p_local.name} (Local)"
    elif freshest_s3:
        default_choice = "S0"
        suggestion = f"{freshest_s3} (S3 - will download to local/)"

    if args.file:
        target = args.file
    else:
        if default_choice:
            print(f"\n✨ Suggested freshest backup: {suggestion}")
            print("Press Enter to use it, or enter another choice (e.g., '1' or 'S1'):")
        else:
            print("\n❌ No backups found in either local or S3 storage.")
            return

        choice = input(f"[{default_choice}] > ").strip()
        if not choice:
            choice = default_choice

        if choice.startswith("S"):
            try:
                idx = int(choice[1:])
                target = download_from_s3(s3_backups[idx])
            except (ValueError, IndexError):
                logger.error(f"Invalid S3 selection: {choice}")
                return
        else:
            try:
                idx = int(choice)
                target = local_backups[idx]
            except (ValueError, IndexError):
                if Path(choice).exists():
                    target = choice
                else:
                    logger.error(f"Invalid local selection: {choice}")
                    return

    archive_path = Path(target)
    if not archive_path.exists() and str(target) in s3_backups:
        archive_path = download_from_s3(str(target))

    if not archive_path.exists():
        logger.error(f"Backup file not found: {archive_path}")
        return

    # 1. Safety check: Disk space (5x archive size)
    if not check_disk_space(archive_path):
        return

    print("\n⚠️  WARNING: This will overwrite your current database and storage files!")
    confirm = input("Are you sure you want to proceed? (y/N): ").strip().lower()
    if confirm != "y":
        print("Restoration cancelled.")
        return

    # 2. Lifecycle: Stop app container
    manage_app_container("stop")

    try:
        logger.info(f"🛠️ Starting restoration from {archive_path.name}")
        extraction_path = TEMP_RESTORE_DIR / "extracted"
        if extraction_path.exists():
            shutil.rmtree(extraction_path)
        extraction_path.mkdir()

        logger.info("🗜️ Extracting archive...")
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=extraction_path)

        extract_root = next(extraction_path.iterdir())

        db_backups = list(extract_root.glob("*.db"))
        if db_backups:
            db_backup = db_backups[0]
            logger.info(f"📦 Restoring SQLite database: {db_backup.name}...")
            DB_PATH.parent.mkdir(exist_ok=True, parents=True)
            shutil.copy2(db_backup, DB_PATH)
            logger.info(f"✅ SQLite restored to {DB_PATH}.")

        logger.info("📂 Restoring static files...")
        for sub_dir in ["transcripts", "voice_samples"]:
            src = extract_root / "storage" / sub_dir
            if src.exists():
                dest = STORAGE_DIR / sub_dir
                shutil.copytree(src, dest, dirs_exist_ok=True)
                logger.info(f"✅ {sub_dir} restored.")

        logger.info("⚙️ Restoring configuration files...")

        # 1. Restore config/ directory files individually to avoid metadata issues
        config_src = extract_root / "config"
        if config_src.exists():
            try:
                config_dest = PROJECT_ROOT / "config"
                config_dest.mkdir(exist_ok=True)
                for f in config_src.glob("*"):
                    if f.is_file():
                        shutil.copy2(f, config_dest / f.name)
                        if f.name == "service-key.json":
                            logger.info("✅ Restored Google Drive service-key.json")
                logger.info("✅ config/ files restored.")
            except Exception as e:
                logger.error(f"❌ Failed to restore config/ files: {e}")
                logger.error("💡 Try running: sudo chown -R $USER:$USER config/")
                raise

        # 2. Restore .env separately
        env_src = extract_root / ".env"
        if env_src.exists():
            dest = PROJECT_ROOT / ".env"
            if dest.exists():
                print("\n❓ File .env already exists in project root.")
                choice = input("   Overwrite .env with version from backup? (y/N): ").strip().lower()
                if choice == "y":
                    shutil.copy2(env_src, dest)
                    logger.info("✅ .env restored.")
                else:
                    logger.info("   Skipped .env restoration.")
            else:
                shutil.copy2(env_src, dest)
                logger.info("✅ .env restored.")

        logger.info("\n✨ RESTORATION FINISHED ✨")

    except Exception as e:
        logger.error(f"💥 RESTORATION FAILURE: {e}")
    finally:
        # 3. Lifecycle: Restart app container
        manage_app_container("start")

        # Cleanup only tmp, keep local/
        if TEMP_RESTORE_DIR.exists():
            logger.info("🧹 Cleaning up temporary extraction files...")
            shutil.rmtree(TEMP_RESTORE_DIR)


if __name__ == "__main__":
    main()
