import json
import logging
import sys
from pathlib import Path

# Добавляем корень проекта в пути
ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_app_settings
from app.transcription.postprocessing import apply_postprocessing_to_raw

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)


def patch_raw_files():
    app_settings = get_app_settings()
    raw_dir = app_settings.raw_transcripts_dir

    if not raw_dir.exists():
        logger.error(f"Raw directory {raw_dir} not found.")
        return

    json_files = list(raw_dir.glob("**/*.json"))
    logger.info(f"Found {len(json_files)} raw JSON files to patch.")

    count = 0
    for file_path in json_files:
        try:
            with open(file_path, encoding="utf-8") as f:
                raw_payload = json.load(f)

            # Применяем пост-процессинг (наши новые правила для "Мастера")
            updated_payload = apply_postprocessing_to_raw(raw_payload)

            # Сохраняем обратно
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(updated_payload, f, ensure_ascii=False, indent=2)

            count += 1
            if count % 50 == 0:
                logger.info(f"Processed {count}/{len(json_files)} files...")

        except Exception as e:
            logger.error(f"Failed to patch {file_path}: {e}")

    logger.info(f"Successfully patched {count} raw files.")


if __name__ == "__main__":
    patch_raw_files()
