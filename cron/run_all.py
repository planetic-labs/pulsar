import logging
import os
import subprocess
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

# Add project root to python path to import app and scripts modules
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Load root .env file
ENV_PATH = ROOT_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)

# Setup logging
LOGS_DIR = ROOT_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)
log_file = LOGS_DIR / "cron_run.log"

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
console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
root_logger.addHandler(console_handler)

logger = logging.getLogger("cron_run")


def send_telegram_alert(errors: list[str]):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("Telegram Bot Token or Chat ID not configured. Skipping alert.")
        return

    logger.info("Consolidating issues and sending Telegram alert...")

    # Compose clean HTML message
    lines = ["<b>⚠️ Обнаружены отклонения при проверке целостности Pulsar!</b>\n"]
    for idx, err in enumerate(errors, 1):
        lines.append(f"{idx}. {err}")
    message = "\n".join(lines)

    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunk_size = 4000
    for i in range(0, len(message), chunk_size):
        chunk = message[i : i + chunk_size]
        payload = {
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        try:
            with httpx.Client() as client:
                res = client.post(url, json=payload, timeout=10.0)
                res.raise_for_status()
        except Exception as e:
            logger.error(f"Failed to send Telegram chunk: {e}")
            break


def main():
    logger.info("=========================================")
    logger.info("🚀 STARTING UNIFIED CRON WORKFLOW")
    logger.info("=========================================")

    # 1. Execute Backup
    logger.info("Step 1/3: Running system backup...")
    try:
        # Run backup script using the current virtualenv executable
        backup_script = ROOT_DIR / "backups" / "backup.py"
        res = subprocess.run([sys.executable, str(backup_script)], capture_output=True, text=True, check=True)
        logger.info("Backup stdout:")
        for line in res.stdout.splitlines():
            logger.info(f"  [BACKUP] {line}")
        logger.info("✅ Backup completed successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Backup failed with exit code {e.returncode}!")
        logger.error(f"Backup stdout:\n{e.stdout}")
        logger.error(f"Backup stderr:\n{e.stderr}")
        # Note: we continue execution of other cron steps even if backup fails

    # 2. Execute Sync Index
    logger.info("Step 2/3: Running index synchronization...")
    try:
        sync_script = ROOT_DIR / "scripts" / "sync_index.py"
        res = subprocess.run([sys.executable, str(sync_script)], capture_output=True, text=True, check=True)
        logger.info("Sync stdout:")
        for line in res.stdout.splitlines():
            logger.info(f"  [SYNC] {line}")
        logger.info("✅ Index synchronization completed successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Index synchronization failed with exit code {e.returncode}!")
        logger.error(f"Sync stdout:\n{e.stdout}")
        logger.error(f"Sync stderr:\n{e.stderr}")

    # 3. Execute Integrity Check
    logger.info("Step 3/3: Running database and index integrity checks...")
    try:
        from scripts.check_integrity import check_integrity

        issues = check_integrity()

        if issues:
            logger.warning(f"❌ Integrity check completed with {len(issues)} issues found!")
            send_telegram_alert(issues)
        else:
            logger.info("✅ Integrity check completed. No issues found.")
    except Exception as e:
        logger.error(f"❌ Integrity check execution failed with error: {e}", exc_info=True)

    logger.info("=========================================")
    logger.info("🎉 UNIFIED CRON WORKFLOW COMPLETED")
    logger.info("=========================================")


if __name__ == "__main__":
    main()
