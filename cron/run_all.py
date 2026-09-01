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
    """Audit dynamic table configuration without deleting generation/rollback tables."""
    logger.info("Step Manticore: Auditing Manticore configuration (read-only)...")
    try:
        compose_cmd = get_docker_compose_cmd()
        # Read manticore.json from manticore container
        cmd = [*compose_cmd, "exec", "-T", "manticore", "cat", "/var/lib/manticore/manticore.json"]
        res = subprocess.run(cmd, capture_output=True, text=True, check=True, timeout=60)
        config = json.loads(res.stdout)

        indexes = config.get("indexes", {})
        generation_tables = sorted(
            name for name in indexes if name != "chunks" and not name.startswith(("chunks_build_", "chunks_retired_"))
        )
        if generation_tables:
            logger.warning("Unexpected Manticore tables found (not modified): %s", generation_tables)
        else:
            logger.info("Manticore configuration is valid; generation and rollback tables are preserved.")
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
    logger.info("Step Integrity: Running read-only database and index audit in container...")
    try:
        res = run_in_container(
            "scripts/verify_integrity_readonly.py",
            "--json",
            "--max-details",
            "20",
            timeout=900,
            check=False,
        )
        if res.returncode not in {0, 1}:
            raise subprocess.CalledProcessError(res.returncode, res.args, output=res.stdout, stderr=res.stderr)

        if res.stderr:
            for line in res.stderr.splitlines():
                logger.info(f"  [INTEGRITY] {line}")
        result: dict[str, Any] = json.loads(res.stdout)
        issues = result.get("issues", [])
        notification_lines: list[str] = []

        if issues:
            logger.warning("❌ Read-only audit found %s issues", len(issues))
            severity = result.get("severity_counts", {})
            categories = result.get("category_counts", {})
            notification_lines.extend(
                [
                    "<b>❌ Read-only аудит Pulsar обнаружил несоответствия.</b>",
                    f"Всего: {len(issues)}; по серьёзности: <code>{json.dumps(severity, ensure_ascii=False)}</code>",
                    f"Категории: <code>{json.dumps(categories, ensure_ascii=False)}</code>",
                    "Данные не изменялись. Подробности находятся в cron-логе.",
                ]
            )
        else:
            logger.info("✅ Read-only audit completed. No issues found.")
            if always_notify:
                notification_lines.append("<b>✅ Read-only аудит Pulsar: ошибок не обнаружено.</b>")

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
