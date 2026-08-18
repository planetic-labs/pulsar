import logging
import os
import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import httpx
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[1]

# Загружаем root .env файл
ENV_PATH = ROOT_DIR / ".env"
if ENV_PATH.exists():
    load_dotenv(ENV_PATH)


class ComposeCmdCache:
    _cached_cmd: ClassVar[list[str] | None] = None

    @classmethod
    def get_cmd(cls) -> list[str]:
        if cls._cached_cmd is not None:
            return cls._cached_cmd

        try:
            subprocess.run(["docker-compose", "--version"], capture_output=True, check=True)
            cls._cached_cmd = ["docker-compose"]
        except Exception:
            cls._cached_cmd = ["docker", "compose"]

        return cls._cached_cmd


def get_docker_compose_cmd() -> list[str]:
    """Возвращает команду для docker compose (docker-compose или docker compose)."""
    return ComposeCmdCache.get_cmd()


def setup_logging(log_name: str) -> logging.Logger:
    """Настраивает логирование в файл и консоль."""
    logs_dir = ROOT_DIR / "logs"
    logs_dir.mkdir(exist_ok=True)
    log_file = logs_dir / f"{log_name}.log"

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

    return logging.getLogger(log_name)


logger = logging.getLogger("cron_lib")


def send_telegram_notification(text: str) -> None:
    """Отправляет уведомление в Telegram-чат частями до 4000 символов."""
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


def run_in_container(script: str, timeout: int = 600) -> subprocess.CompletedProcess[str]:
    """Выполняет Python-скрипт внутри контейнера pulsar с таймаутом."""
    compose_cmd = get_docker_compose_cmd()
    cmd = [*compose_cmd, "exec", "-T", "pulsar", "uv", "run", "python", script]
    return subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=timeout)


def is_worker_active() -> tuple[bool, int]:
    """Проверяет, есть ли активные или ожидающие задачи в очереди воркера (внутри контейнера)."""
    try:
        compose_cmd = get_docker_compose_cmd()
        # Код Python для выполнения внутри контейнера, чтобы избежать импорта app на хосте
        py_code = (
            "import sys\n"
            "from app.config import get_sqlite_settings\n"
            "from app.db import db_connection\n"
            "settings = get_sqlite_settings()\n"
            "with db_connection(settings) as conn:\n"
            "    r = conn.execute(\n"
            "        \"SELECT COUNT(*) as cnt FROM tasks WHERE status IN ('pending', 'running')\"\n"
            "    ).fetchone()\n"
            "    print(r['cnt'] if r else 0)\n"
        )
        cmd = [*compose_cmd, "exec", "-T", "pulsar", "uv", "run", "python", "-c", py_code]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=30)
        output = res.stdout.strip()
        count = int(output) if output.isdigit() else 0
        return count > 0, count
    except Exception as e:
        logger.error(f"Failed to check worker status in container: {e}")
        # Из соображений безопасности считаем, что воркер активен, если проверка упала
        return True, 0
