import datetime
import logging
import os
import shutil
import sqlite3
import subprocess
import sys
import tarfile
from pathlib import Path
from typing import cast

import boto3
import httpx
from botocore.exceptions import ClientError
from dotenv import load_dotenv

# Setup clean logging
logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger("backup")

# Load local .env file
ENV_PATH = Path(__file__).parent / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)
else:
    logger.warning(f"Local .env file not found in {ENV_PATH.parent}. Using system environment variables.")

# Configuration
BACKUP_TOOL_DIR = Path(__file__).parent.resolve()
PROJECT_ROOT = BACKUP_TOOL_DIR.parent
DATA_DIR = Path(os.getenv("DATA_DIR", PROJECT_ROOT / "data"))
STORAGE_DIR = Path(os.getenv("STORAGE_DIR", PROJECT_ROOT / "storage"))

LOCAL_BACKUP_DIR = BACKUP_TOOL_DIR / "local"
TEMP_DIR = BACKUP_TOOL_DIR / "tmp"

# Auto-create folders at start
LOCAL_BACKUP_DIR.mkdir(exist_ok=True, parents=True)
TEMP_DIR.mkdir(exist_ok=True, parents=True)

# App Config
DB_PATH = DATA_DIR / "search_ui.db"
QDRANT_URL = os.getenv("QDRANT_URL", "http://127.0.0.1:6333")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "chunks_m3")
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
BACKUP_PREFIX = f"videodb_backup_{TIMESTAMP}"
BACKUP_CONTENT_DIR = TEMP_DIR / BACKUP_PREFIX

# S3 Config
S3_ENDPOINT = os.getenv("S3_ENDPOINT_URL")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY")
S3_BUCKET = os.getenv("S3_BUCKET_NAME")
S3_REGION = os.getenv("S3_REGION_NAME", "us-east-1")

# Retention Config (S3 only)
MAX_BACKUPS_S3 = int(os.getenv("MAX_BACKUPS", "10"))


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


def check_disk_space():
    """Check if there is enough space (2x current data)."""
    total_size = 0
    if DB_PATH.exists():
        total_size += DB_PATH.stat().st_size
    for sub in ["transcripts", "voice_samples"]:
        p = STORAGE_DIR / sub
        if p.exists():
            total_size += sum(f.stat().st_size for f in p.glob("**/*") if f.is_file())

    required_space = total_size * 2
    usage = shutil.disk_usage(BACKUP_TOOL_DIR)

    if usage.free < required_space:
        req_mb = required_space / 1024 / 1024
        free_mb = usage.free / 1024 / 1024
        logger.error(f"❌ Not enough disk space! Required: {req_mb:.2f} MB, Free: {free_mb:.2f} MB")
        return False
    return True


def manage_app_container(action: str):
    """Start or stop the app container using docker compose."""
    compose_file = os.getenv("COMPOSE_FILE", "docker-compose.yml")
    cmd = ["docker", "compose", "-f", compose_file, action, "app"]
    logger.info(f"🐳 Docker: {action}ing app container using {compose_file}...")
    try:
        subprocess.run(cmd, cwd=PROJECT_ROOT, check=True, capture_output=True)
        logger.info(f"✅ App container {action}ed successfully.")
    except subprocess.CalledProcessError as e:
        logger.warning(f"⚠️ Failed to {action} app container: {e.stderr.decode().strip()}")


def cleanup_local_backups(keep_file: Path):
    """Keep only the latest backup in local/ folder."""
    logger.info("🧹 Cleaning up old local backups...")
    local_backups = sorted(LOCAL_BACKUP_DIR.glob("videodb_backup_*.tar.gz"), key=os.path.getmtime, reverse=True)
    for old_backup in local_backups:
        p = Path(cast(str, old_backup))
        if p.name != keep_file.name:
            logger.info(f"Removing old local backup: {p.name}")
            p.unlink()


def cleanup_s3_backups():
    """Cleanup S3 based on MAX_BACKUPS."""
    s3_client = get_s3_client()
    if not s3_client:
        return
    logger.info(f"🧹 Cleaning up S3 backups (keeping last {MAX_BACKUPS_S3})...")
    try:
        res = s3_client.list_objects_v2(Bucket=S3_BUCKET, Prefix="videodb_backup_")
        if "Contents" in res:
            objects = sorted(res["Contents"], key=lambda x: x["LastModified"], reverse=True)
            if len(objects) > MAX_BACKUPS_S3:
                for old_obj in objects[MAX_BACKUPS_S3:]:
                    logger.info(f"Removing old S3 backup: {old_obj['Key']}")
                    s3_client.delete_object(Bucket=S3_BUCKET, Key=old_obj["Key"])
    except Exception as e:
        logger.error(f"❌ S3 cleanup failed: {e}")


def upload_to_s3(file_path: Path):
    s3_client = get_s3_client()
    if not s3_client:
        logger.error("❌ S3 credentials not configured. Cannot upload.")
        raise RuntimeError("S3 configuration missing")

    file_size = file_path.stat().st_size
    uploaded = 0

    def progress(bytes_amount):
        nonlocal uploaded
        uploaded += bytes_amount
        percent = (uploaded / file_size) * 100
        sys.stdout.write(f"\r☁️ Uploading to S3: {percent:.1f}% ({uploaded/1024/1024:.1f} MB)")
        sys.stdout.flush()

    logger.info(f"☁️ Uploading {file_path.name} to bucket '{S3_BUCKET}'...")
    try:
        s3_client.upload_file(str(file_path), S3_BUCKET, file_path.name, Callback=progress)
        sys.stdout.write("\n")
        logger.info("✅ S3 upload successful.")
        return True
    except ClientError as e:
        sys.stdout.write("\n")
        logger.error(f"❌ S3 upload failed: {e}")
        raise


def backup_sqlite(dest_dir: Path):
    logger.info("📦 Backing up SQLite database...")
    dest_path = dest_dir / "search_ui.db"
    if not DB_PATH.exists():
        logger.error(f"❌ Database not found at {DB_PATH}")
        raise FileNotFoundError(f"Database not found at {DB_PATH}")

    source = sqlite3.connect(DB_PATH)
    dest = sqlite3.connect(dest_path)
    try:
        with dest:
            source.backup(dest)
        logger.info("✅ SQLite backup complete.")
    except Exception as e:
        logger.error(f"❌ SQLite backup failed: {e}")
        raise
    finally:
        dest.close()
        source.close()


def backup_qdrant(dest_dir: Path):
    logger.info("📡 Creating Qdrant snapshot...")
    try:
        with httpx.Client(timeout=600.0) as client:
            res = client.post(f"{QDRANT_URL}/collections/{COLLECTION_NAME}/snapshots")
            res.raise_for_status()
            snapshot_name = res.json()["result"]["name"]
            logger.info(f"✅ Snapshot created: {snapshot_name}")

            logger.info("⬇️ Downloading snapshot...")
            snap_path = dest_dir / snapshot_name
            with client.stream("GET", f"{QDRANT_URL}/collections/{COLLECTION_NAME}/snapshots/{snapshot_name}") as r:
                r.raise_for_status()
                with open(snap_path, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=65536):
                        f.write(chunk)
            logger.info("✅ Snapshot downloaded.")

            client.delete(f"{QDRANT_URL}/collections/{COLLECTION_NAME}/snapshots/{snapshot_name}")
            logger.info("✅ Qdrant storage cleaned up.")
    except Exception as e:
        logger.error(f"❌ Qdrant backup failed: {e}")
        raise


def copy_files(dest_dir: Path):
    logger.info("📂 Copying static files and configuration...")
    try:
        # 1. Copy storage subdirectories
        for sub_dir in ["transcripts", "voice_samples"]:
            src = STORAGE_DIR / sub_dir
            if src.exists():
                shutil.copytree(src, dest_dir / "storage" / sub_dir, dirs_exist_ok=True)
        
        # 2. Copy the entire config/ directory
        config_src = PROJECT_ROOT / "config"
        if config_src.exists():
            shutil.copytree(config_src, dest_dir / "config", dirs_exist_ok=True)
            
        # 3. Copy .env from project root
        env_src = PROJECT_ROOT / ".env"
        if env_src.exists():
            shutil.copy2(env_src, dest_dir / ".env")
            
        logger.info("✅ Configuration and data copy complete.")
    except Exception as e:
        logger.error(f"❌ Files copy failed: {e}")
        raise


def create_archive(source_dir: Path):
    archive_path = LOCAL_BACKUP_DIR / f"{BACKUP_PREFIX}.tar.gz"
    logger.info(f"🗜️ Creating compressed archive: {archive_path.name}...")
    try:
        with tarfile.open(archive_path, "w:gz") as tar:
            tar.add(source_dir, arcname=BACKUP_PREFIX)
        logger.info(f"✅ Archive created. Size: {archive_path.stat().st_size / 1024 / 1024:.2f} MB")
        return archive_path
    except Exception as e:
        logger.error(f"❌ Archive creation failed: {e}")
        raise


def main():
    logger.info(f"🚀 Starting backup process at {TIMESTAMP}")

    if not check_disk_space():
        exit(1)

    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)
    TEMP_DIR.mkdir(parents=True)
    BACKUP_CONTENT_DIR.mkdir()

    try:
        backup_sqlite(BACKUP_CONTENT_DIR)
        backup_qdrant(BACKUP_CONTENT_DIR)
        copy_files(BACKUP_CONTENT_DIR)
        archive_path = create_archive(BACKUP_CONTENT_DIR)
        upload_to_s3(archive_path)

        cleanup_local_backups(archive_path)
        cleanup_s3_backups()

        logger.info("🧹 Cleaning up temporary files...")
        shutil.rmtree(TEMP_DIR)
        logger.info("\n✨ BACKUP FINISHED SUCCESSFULLY ✨")
        logger.info(f"Local file: {archive_path.name}")

    except Exception as e:
        logger.error(f"💥 GLOBAL BACKUP FAILURE: {e}")
        if TEMP_DIR.exists():
            shutil.rmtree(TEMP_DIR)
        exit(1)


if __name__ == "__main__":
    main()
