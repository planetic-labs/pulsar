import json
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
log_file = LOGS_DIR / "cron_integrity_check.log"

root_logger = logging.getLogger()
root_logger.setLevel(logging.INFO)
for h in root_logger.handlers[:]:
    root_logger.removeHandler(h)

# File handler
file_handler = logging.FileHandler(log_file, mode="w", encoding="utf-8")
file_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
root_logger.addHandler(file_handler)

# Console handler
console_handler = logging.StreamHandler(sys.stdout)
console_handler.setFormatter(logging.Formatter("%(asctime)s - %(levelname)s - %(message)s"))
root_logger.addHandler(console_handler)

logger = logging.getLogger("cron_integrity_check")


def send_telegram_notification(text: str):
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    chat_id = os.getenv("TELEGRAM_CHAT_ID")
    if not token or not chat_id:
        logger.warning("Telegram Bot Token or Chat ID not configured. Skipping notification.")
        return

    logger.info("Sending Telegram notification...")
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunk_size = 4000
    for i in range(0, len(text), chunk_size):
        chunk = text[i : i + chunk_size]
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


def get_docker_compose_cmd() -> list[str]:
    try:
        subprocess.run(["docker-compose", "--version"], capture_output=True, check=True)
        return ["docker-compose"]
    except Exception:
        return ["docker", "compose"]


def main():
    logger.info("=========================================")
    logger.info("🚀 STARTING INTEGRITY CHECK CRON JOB")
    logger.info("=========================================")

    try:
        compose_cmd = get_docker_compose_cmd()
        py_cmd = (
            "from scripts.verify_integrity import verify_integrity; "
            "import json; "
            "print('INTEGRITY_ISSUES:' + json.dumps(verify_integrity()))"
        )
        cmd = compose_cmd + ["exec", "-T", "pulsar", "uv", "run", "python", "-c", py_cmd]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True)

        # Log stdout lines
        logger.info("Integrity check stdout:")
        for line in res.stdout.splitlines():
            if not line.startswith("INTEGRITY_ISSUES:"):
                logger.info(f"  [INTEGRITY] {line}")

        # Parse result
        result = {}
        for line in res.stdout.splitlines():
            if line.startswith("INTEGRITY_ISSUES:"):
                json_str = line[len("INTEGRITY_ISSUES:") :]
                result = json.loads(json_str)
                break

        status = result.get("status")
        if status == "worker_running":
            tasks_count = result.get("active_tasks_count", 0)
            msg = (
                "<b>ℹ️ Проверка целостности Pulsar отложена:</b> "
                f"воркер сейчас занят обработкой задач (в очереди: {tasks_count})."
            )
            logger.info(f"Worker is active ({tasks_count} tasks in queue). Postponing integrity check.")
            send_telegram_notification(msg)
            return

        issues = result.get("issues", [])
        deleted_raw = result.get("deleted_raw_count", 0)
        deleted_norm = result.get("deleted_norm_count", 0)
        reindexed_videos = result.get("reindexed_videos_count", 0)
        reindexed_chunks = result.get("reindexed_chunks_count", 0)
        deleted_manticore_points = result.get("deleted_manticore_points_count", 0)

        # Build notification text
        notification_lines = []

        if issues:
            logger.warning(f"❌ Integrity check completed with {len(issues)} issues found!")
            notification_lines.append("<b>⚠️ Обнаружены отклонения при проверке целостности Pulsar!</b>\n")
            for idx, err in enumerate(issues, 1):
                notification_lines.append(f"{idx}. {err}")
            notification_lines.append("")
        else:
            logger.info("✅ Integrity check completed. No critical issues found.")
            notification_lines.append("<b>✅ Проверка целостности Pulsar завершена.</b>\n")

        auto_corrected = []
        if deleted_raw > 0:
            auto_corrected.append(f"• Удалено сиротских RAW-файлов: {deleted_raw}")
        if deleted_norm > 0:
            auto_corrected.append(f"• Удалено сиротских NORMALIZED-файлов: {deleted_norm}")
        if reindexed_videos > 0:
            auto_corrected.append(
                f"• Отправлено на повторную индексацию: {reindexed_videos} видео ({reindexed_chunks} чанков)"
            )
        if deleted_manticore_points > 0:
            auto_corrected.append(f"• Удалено сиротских векторов из Manticore: {deleted_manticore_points}")

        if auto_corrected:
            notification_lines.append("<b>🔄 Автоматически исправлено:</b>")
            notification_lines.extend(auto_corrected)
        elif not issues:
            notification_lines.append("Ошибок не обнаружено!")

        send_telegram_notification("\n".join(notification_lines))

    except Exception as e:
        logger.error(f"❌ Integrity check execution failed with error: {e}", exc_info=True)
        error_details = str(e)
        if isinstance(e, subprocess.CalledProcessError):
            error_details = f"Exit code: {e.returncode}\nStderr:\n{e.stderr}\nStdout:\n{e.stdout}"

        err_msg = f"<b>❌ Сбой при запуске проверки целостности Pulsar!</b>\n\nОшибка:\n<code>{error_details}</code>"
        send_telegram_notification(err_msg)

    logger.info("=========================================")
    logger.info("🎉 INTEGRITY CHECK CRON JOB COMPLETED")
    logger.info("=========================================")


if __name__ == "__main__":
    main()
