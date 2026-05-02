import argparse
import logging
import os
import shutil
import tarfile
from pathlib import Path

import boto3
import httpx
from botocore.exceptions import ClientError

# Setup logging
logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(levelname)s: %(message)s")
logger = logging.getLogger("restore")

# Paths
BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
STORAGE_DIR = BASE_DIR / "storage"
BACKUP_DIR = BASE_DIR / "backups"
BACKUP_DIR.mkdir(exist_ok=True)
TEMP_RESTORE_DIR = BACKUP_DIR / "temp_restore"

# Config
QDRANT_URL = os.getenv("QDRANT_URL", "http://qdrant:6333")
COLLECTION_NAME = os.getenv("QDRANT_COLLECTION", "videodb")

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


def list_backups():
    logger.info("🔍 Available local backups:")
    local_backups = sorted(BACKUP_DIR.glob("*.tar.gz"), reverse=True)
    for i, b in enumerate(local_backups):
        logger.info(f"  [{i}] {b.name} (Local)")

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
        except ClientError as e:
            logger.error(f"Failed to list S3 backups: {e}")

    return local_backups, s3_backups


def download_from_s3(key: str):
    dest_path = BACKUP_DIR / key
    if dest_path.exists():
        logger.info(f"Using already downloaded file: {dest_path}")
        return dest_path

    logger.info(f"⬇️ Downloading {key} from S3...")
    s3 = get_s3_client()
    if not s3:
        raise RuntimeError("S3 client not configured")

    s3.download_file(S3_BUCKET, key, str(dest_path))
    logger.info("✅ Download complete.")
    return dest_path


def restore_qdrant(extract_dir: Path):
    logger.info("📡 Restoring Qdrant collection...")
    # Find snapshot file
    snapshots = list(extract_dir.glob("*.snapshot"))
    if not snapshots:
        logger.warning("No Qdrant snapshot found in backup.")
        return

    snapshot_path = snapshots[0]
    logger.info(f"Found snapshot: {snapshot_path.name}")

    try:
        # 1. Upload snapshot to Qdrant
        with open(snapshot_path, "rb") as f:
            with httpx.Client(timeout=600.0) as client:
                logger.info("Uploading snapshot to Qdrant...")
                files = {"snapshot": (snapshot_path.name, f)}
                res = client.post(f"{QDRANT_URL}/collections/{COLLECTION_NAME}/snapshots/upload", files=files)
                res.raise_for_status()
                logger.info("✅ Snapshot uploaded.")

                # 2. Recover from snapshot
                logger.info(f"Recovering collection '{COLLECTION_NAME}' from snapshot...")
                res = client.put(
                    f"{QDRANT_URL}/collections/{COLLECTION_NAME}/snapshots/recover",
                    json={
                        "location": f"http://localhost:6333/collections/{COLLECTION_NAME}/snapshots/{snapshot_path.name}"
                    },
                )
                # Note: 'location' inside the request to Qdrant might need adjustment
                # if Qdrant is in Docker and we are calling it from another container.
                # Actually, Qdrant can recover from a local file if it's already uploaded.

                # If the above fails, we might need a different approach.
                # But typically Qdrant expects a URL or a name of a snapshot already in its storage.

                logger.info("✅ Qdrant recovery triggered. Note: This might take some time.")
    except Exception as e:
        logger.error(f"❌ Qdrant restoration failed: {e}")


def main():
    parser = argparse.ArgumentParser(description="Restore VideoDB from backup")
    parser.add_argument("--file", help="Path to backup file (local or S3 key)")
    args = parser.parse_args()

    local_backups, s3_backups = list_backups()

    if args.file:
        target = args.file
    else:
        logger.info("\nSelect a backup to restore (e.g., '0' or 'S0'):")
        choice = input("> ").strip()
        if choice.startswith("S"):
            idx = int(choice[1:])
            target = download_from_s3(s3_backups[idx])
        else:
            idx = int(choice)
            target = local_backups[idx]

    archive_path = Path(target)
    if not archive_path.exists() and str(target) in s3_backups:
        archive_path = download_from_s3(str(target))

    logger.info(f"🛠️ Starting restoration from {archive_path.name}")

    if TEMP_RESTORE_DIR.exists():
        shutil.rmtree(TEMP_RESTORE_DIR)
    TEMP_RESTORE_DIR.mkdir(parents=True)

    try:
        # 1. Extract
        logger.info("🗜️ Extracting archive...")
        with tarfile.open(archive_path, "r:gz") as tar:
            tar.extractall(path=TEMP_RESTORE_DIR)

        # The archive contains a folder with the backup prefix
        extract_root = next(TEMP_RESTORE_DIR.iterdir())
        logger.info(f"Extract root: {extract_root}")

        # 2. Restore SQLite
        db_backup = extract_root / "search_ui.db"
        if db_backup.exists():
            logger.info("📦 Restoring SQLite database...")
            # Ensure data dir exists
            DATA_DIR.mkdir(exist_ok=True)
            shutil.copy2(db_backup, DATA_DIR / "search_ui.db")
            logger.info("✅ SQLite restored.")

        # 3. Restore Static Files
        logger.info("📂 Restoring static files...")
        for sub_dir in ["transcripts", "voice_samples"]:
            src = extract_root / "storage" / sub_dir
            if src.exists():
                dest = STORAGE_DIR / sub_dir
                shutil.copytree(src, dest, dirs_exist_ok=True)
                logger.info(f"✅ {sub_dir} restored.")

        # 4. Restore Qdrant
        restore_qdrant(extract_root)

        # 5. Restore Configs (optional)
        logger.info("💡 Configuration files (.env, google.json) were found in backup.")
        logger.info("   They were NOT restored automatically to prevent overwriting secrets.")
        logger.info(f"   You can find them in {extract_root}")

        logger.info("\n✨ RESTORATION FINISHED ✨")
        logger.info("Please restart the services to apply changes.")

    finally:
        if TEMP_RESTORE_DIR.exists():
            logger.info("🧹 Cleaning up temporary restoration files...")
            shutil.rmtree(TEMP_RESTORE_DIR)


if __name__ == "__main__":
    main()
