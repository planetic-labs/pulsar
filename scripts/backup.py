import datetime
import logging
import os
import shutil
import sqlite3
import tarfile
from pathlib import Path

import boto3
import httpx
from botocore.exceptions import ClientError

# Setup logging
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("backup")

# Paths
BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
STORAGE_DIR = BASE_DIR / "storage"
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True)

# Config
DB_PATH = DATA_DIR / "search_ui.db"
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "videodb")
TIMESTAMP = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
BACKUP_PREFIX = f"videodb_backup_{TIMESTAMP}"
TEMP_DIR = BACKUP_DIR / BACKUP_PREFIX

# S3 Config
S3_ENDPOINT = os.getenv("S3_ENDPOINT_URL")
S3_ACCESS_KEY = os.getenv("S3_ACCESS_KEY")
S3_SECRET_KEY = os.getenv("S3_SECRET_KEY")
S3_BUCKET = os.getenv("S3_BUCKET_NAME")
S3_REGION = os.getenv("S3_REGION_NAME", "us-east-1")


def upload_to_s3(file_path: Path):
    if not all([S3_ENDPOINT, S3_ACCESS_KEY, S3_SECRET_KEY, S3_BUCKET]):
        logger.warning("⚠️ S3 credentials not fully configured. Skipping upload.")
        return False

    logger.info(f"☁️ Uploading {file_path.name} to S3 bucket '{S3_BUCKET}'...")
    try:
        s3_client = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            aws_access_key_id=S3_ACCESS_KEY,
            aws_secret_access_key=S3_SECRET_KEY,
            region_name=S3_REGION,
        )
        s3_client.upload_file(str(file_path), S3_BUCKET, file_path.name)
        logger.info("✅ S3 upload successful.")
        return True
    except ClientError as e:
        logger.error(f"❌ S3 upload failed: {e}")
        return False


def backup_sqlite(dest_dir: Path):
    logger.info("📦 Backing up SQLite database...")
    dest_path = dest_dir / "search_ui.db"

    if not DB_PATH.exists():
        logger.warning(f"Database not found at {DB_PATH}, skipping SQLite backup.")
        return

    source = sqlite3.connect(DB_PATH)
    dest = sqlite3.connect(dest_path)
    try:
        with dest:
            source.backup(dest)
        logger.info("✅ SQLite backup complete.")
    finally:
        dest.close()
        source.close()


def backup_qdrant(dest_dir: Path):
    logger.info(f"📡 Creating Qdrant snapshot for collection '{COLLECTION_NAME}'...")
    try:
        with httpx.Client(timeout=600.0) as client:
            # Create snapshot
            logger.info(f"POST {QDRANT_URL}/collections/{COLLECTION_NAME}/snapshots")
            res = client.post(f"{QDRANT_URL}/collections/{COLLECTION_NAME}/snapshots")

            if res.status_code == 404:
                logger.warning(f"Collection '{COLLECTION_NAME}' not found in Qdrant, skipping vectors backup.")
                return

            res.raise_for_status()
            snapshot_name = res.json()["result"]["name"]
            logger.info(f"✅ Snapshot created: {snapshot_name}")

            # Download snapshot
            logger.info("⬇️ Downloading snapshot...")
            snap_path = dest_dir / snapshot_name
            with client.stream("GET", f"{QDRANT_URL}/collections/{COLLECTION_NAME}/snapshots/{snapshot_name}") as r:
                r.raise_for_status()
                with open(snap_path, "wb") as f:
                    for chunk in r.iter_bytes(chunk_size=65536):
                        f.write(chunk)
            logger.info(f"✅ Snapshot downloaded to {snap_path}")

            # Clean up Qdrant storage
            logger.info("🧹 Deleting snapshot from Qdrant storage...")
            client.delete(f"{QDRANT_URL}/collections/{COLLECTION_NAME}/snapshots/{snapshot_name}")
            logger.info("✅ Qdrant storage cleaned up.")

    except Exception as e:
        logger.error(f"❌ Qdrant backup failed: {e}")
        # We don't raise here to allow other parts of the backup to proceed


def copy_files(dest_dir: Path):
    logger.info("📂 Copying static files and configuration...")

    # Transcripts
    transcripts_src = STORAGE_DIR / "transcripts"
    if transcripts_src.exists():
        logger.info(f"Copying transcripts from {transcripts_src}")
        shutil.copytree(transcripts_src, dest_dir / "storage" / "transcripts", dirs_exist_ok=True)

    # Voice Samples
    voices_src = STORAGE_DIR / "voice_samples"
    if voices_src.exists():
        logger.info(f"Copying voice samples from {voices_src}")
        shutil.copytree(voices_src, dest_dir / "storage" / "voice_samples", dirs_exist_ok=True)

    # Configs
    logger.info("Copying configuration files (.env, google.json)...")
    for f_name in [".env", "google.json"]:
        src = BASE_DIR / f_name
        if src.exists():
            shutil.copy2(src, dest_dir / f_name)

    logger.info("✅ Static files copy complete.")


def create_archive(source_dir: Path):
    archive_path = BACKUP_DIR / f"{BACKUP_PREFIX}.tar.gz"
    logger.info(f"🗜️ Creating compressed archive: {archive_path.name}...")

    with tarfile.open(archive_path, "w:gz") as tar:
        # Add everything inside source_dir to the archive
        tar.add(source_dir, arcname=BACKUP_PREFIX)

    logger.info(f"✅ Archive created successfully. Final size: {archive_path.stat().st_size / 1024 / 1024:.2f} MB")
    return archive_path


def main():
    logger.info(f"🚀 Starting backup process for VideoDB AI at {TIMESTAMP}")

    if TEMP_DIR.exists():
        shutil.rmtree(TEMP_DIR)
    TEMP_DIR.mkdir(parents=True)

    try:
        # 1. SQLite
        backup_sqlite(TEMP_DIR)

        # 2. Qdrant
        backup_qdrant(TEMP_DIR)

        # 3. Static Files
        copy_files(TEMP_DIR)

        # 4. Compress
        archive_path = create_archive(TEMP_DIR)

        # 5. S3 Upload
        upload_to_s3(archive_path)

        # 6. Cleanup
        logger.info("🧹 Cleaning up temporary files...")
        shutil.rmtree(TEMP_DIR)

        logger.info("\n✨ BACKUP FINISHED SUCCESSFULLY ✨")
        logger.info(f"Location: {archive_path}")

    except Exception as e:
        logger.error(f"💥 GLOBAL BACKUP FAILURE: {e}")
        if TEMP_DIR.exists():
            shutil.rmtree(TEMP_DIR)
        exit(1)


if __name__ == "__main__":
    main()
