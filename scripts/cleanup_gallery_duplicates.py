from __future__ import annotations

import sys
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.file_dedupe import has_gallery_marker

TARGET_DIRS = [
    ROOT_DIR / "downloads",
    ROOT_DIR / "audio",
    ROOT_DIR / "transcripts",
]


def main() -> None:
    deleted = 0
    for directory in TARGET_DIRS:
        if not directory.exists():
            continue
        for path in sorted(directory.iterdir()):
            if not path.is_file():
                continue
            if not path.name.startswith("24."):
                continue
            if not has_gallery_marker(path.name):
                continue
            path.unlink(missing_ok=True)
            deleted += 1
            print(f"Deleted: {path}")
    print(f"Deleted files: {deleted}")


if __name__ == "__main__":
    main()
