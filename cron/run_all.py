#!/usr/bin/env python3
import argparse
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

# Add project root to python path to import cron modules
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from cron._lib import (  # noqa: E402
    get_docker_compose_cmd,
    is_worker_active,
    run_in_container,
    send_telegram_notification,
    setup_logging,
)

logger = setup_logging("cron_run")


def clean_manticore_json() -> None:
    logger.info("Step Manticore: Checking Manticore configuration integrity (manticore.json)...")
    try:
        compose_cmd = get_docker_compose_cmd()
        # Read manticore.json from manticore container
        cmd = compose_cmd + ["exec", "-T", "manticore", "cat", "/var/lib/manticore/manticore.json"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
        config = json.loads(res.stdout)

        # We only want 'chunks' index in configuration. If other indexes exist, clean them up.
        indexes = config.get("indexes", {})
        dirty = False
        allowed_indexes = {"chunks"}

        for idx_name in list(indexes.keys()):
            if idx_name not in allowed_indexes:
                logger.warning(f"Found orphaned index '{idx_name}' in manticore.json. Removing it...")
                indexes.pop(idx_name)
                dirty = True

        if dirty:
            # Write cleaned configuration back using cat to prevent shell injection
            cleaned_json = json.dumps(config, separators=(",", ":"))
            write_cmd = compose_cmd + [
                "exec",
                "-T",
                "manticore",
                "sh",
                "-c",
                "cat > /var/lib/manticore/manticore.json",
            ]
            subprocess.run(write_cmd, input=cleaned_json, text=True, check=True, timeout=60)
            logger.info("Successfully cleaned manticore.json. Restarting Manticore container to apply changes...")

            # Restart manticore to apply changes
            restart_cmd = compose_cmd + ["restart", "manticore"]
            subprocess.run(restart_cmd, check=True, timeout=120)
            logger.info("Manticore container restarted and configuration applied.")
        else:
            logger.info("Manticore configuration is clean.")
    except Exception as e:
        logger.error(f"Failed to check/clean manticore.json: {e}")
        raise


def run_cleanup() -> None:
    logger.info("Step Cleanup: Running tasks history cleanup in container...")
    try:
        res = run_in_container("scripts/cleanup_tasks.py", timeout=300)
        logger.info("Cleanup stdout:")
        for line in res.stdout.splitlines():
            logger.info(f"  [CLEANUP] {line}")
        logger.info("✅ Tasks cleanup completed successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Tasks cleanup failed with exit code {e.returncode}!")
        logger.error(f"Cleanup stdout:\n{e.stdout}")
        logger.error(f"Cleanup stderr:\n{e.stderr}")
        raise
    except Exception as e:
        logger.error(f"❌ Tasks cleanup execution failed: {e}")
        raise


def run_integrity(always_notify: bool = False) -> None:
    logger.info("Step Integrity: Running database and index integrity checks in container...")
    try:
        res = run_in_container("scripts/verify_integrity.py", timeout=900)

        # Log stdout/stderr lines except the JSON wrapper line
        logger.info("Integrity check stdout:")
        for line in res.stdout.splitlines():
            if not line.startswith("INTEGRITY_ISSUES:"):
                logger.info(f"  [INTEGRITY] {line}")

        # Parse result
        result: dict[str, Any] = {}
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
            notification_lines.append(result.get("summary") or "")
            notification_lines.append("")
        else:
            logger.info("✅ Integrity check completed. No critical issues found.")

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
            if not issues:
                notification_lines.append("<b>✅ Проверка целостности Pulsar завершена.</b>\n")
            notification_lines.append("<b>🔄 Автоматически исправлено:</b>")
            notification_lines.extend(auto_corrected)
        elif not issues and always_notify:
            notification_lines.append("<b>✅ Проверка целостности Pulsar завершена.</b>\n")
            notification_lines.append("Ошибок не обнаружено!")

        if notification_lines:
            send_telegram_notification("\n".join(notification_lines))

    except Exception as e:
        logger.error(f"❌ Integrity check execution failed with error: {e}", exc_info=True)
        error_details = str(e)
        if isinstance(e, subprocess.CalledProcessError):
            logger.error(f"Integrity check stdout:\n{e.stdout}")
            logger.error(f"Integrity check stderr:\n{e.stderr}")
            error_details = f"Exit code: {e.returncode}\nStderr:\n{e.stderr}\nStdout:\n{e.stdout}"

        err_msg = f"<b>❌ Сбой при запуске проверки целостности Pulsar!</b>\n\nОшибка:\n<code>{error_details}</code>"
        send_telegram_notification(err_msg)
        raise


def run_sync() -> None:
    logger.info("Step Sync: Running index synchronization in container...")
    try:
        res = run_in_container("scripts/sync_index.py", timeout=1200)
        logger.info("Sync stdout:")
        for line in res.stdout.splitlines():
            logger.info(f"  [SYNC] {line}")
        logger.info("✅ Index synchronization completed successfully.")
    except subprocess.CalledProcessError as e:
        logger.error(f"❌ Index synchronization failed with exit code {e.returncode}!")
        logger.error(f"Sync stdout:\n{e.stdout}")
        logger.error(f"Sync stderr:\n{e.stderr}")
        raise
    except Exception as e:
        logger.error(f"❌ Index synchronization execution failed: {e}")
        raise


def main() -> None:
    parser = argparse.ArgumentParser(description="Unified Cron Workflow for Pulsar")
    parser.add_argument(
        "--step",
        choices=["all", "manticore", "cleanup", "integrity", "sync"],
        default="all",
        help="Запустить конкретный шаг или все шаги (по умолчанию: all)",
    )
    parser.add_argument(
        "--always-notify",
        action="store_true",
        help="Всегда отправлять Telegram-уведомление для шага integrity, даже если ошибок нет",
    )
    args = parser.parse_args()

    logger.info("=========================================")
    logger.info(f"🚀 STARTING UNIFIED CRON WORKFLOW (step: {args.step})")
    logger.info("=========================================")

    # Проверяем активность воркера перед выполнением любых операций
    active, tasks_count = is_worker_active()
    if active:
        msg = (
            "<b>ℹ️ Выполнение регламентных задач Pulsar отложено:</b> "
            f"воркер сейчас занят обработкой задач (в очереди: {tasks_count})."
        )
        logger.info(f"Worker is active ({tasks_count} tasks in queue). Postponing all cron steps.")
        send_telegram_notification(msg)
        sys.exit(0)

    has_errors = False

    if args.step == "all":
        # Шаг Manticore
        try:
            clean_manticore_json()
        except Exception:
            has_errors = True

        # Шаг Cleanup
        try:
            run_cleanup()
        except Exception:
            has_errors = True

        # Шаг Integrity
        try:
            run_integrity(always_notify=args.always_notify)
        except Exception:
            has_errors = True

        # Шаг Sync
        try:
            run_sync()
        except Exception:
            has_errors = True
    else:
        try:
            if args.step == "manticore":
                clean_manticore_json()
            elif args.step == "cleanup":
                run_cleanup()
            elif args.step == "integrity":
                run_integrity(always_notify=True)  # При явном вызове integrity всегда уведомляем
            elif args.step == "sync":
                run_sync()
        except Exception:
            has_errors = True

    if has_errors:
        logger.error("=========================================")
        logger.error("❌ UNIFIED CRON WORKFLOW COMPLETED WITH ERRORS")
        logger.error("=========================================")
        sys.exit(1)
    else:
        logger.info("=========================================")
        logger.info("🎉 UNIFIED CRON WORKFLOW COMPLETED SUCCESSFULLY")
        logger.info("=========================================")
        sys.exit(0)


if __name__ == "__main__":
    main()
