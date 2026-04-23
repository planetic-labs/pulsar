import sys
from pathlib import Path

# Add project root to path
ROOT_DIR = Path(__file__).resolve().parents[0]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from app.config import get_google_drive_settings
from app.google_drive import GoogleDriveClient


def test_drive():
    settings = get_google_drive_settings()
    client = GoogleDriveClient(settings)

    print("Testing Google Drive connection...")
    try:
        # Try to list root
        items = client.list_folder_contents("root")
        print(f"Success! Found {len(items)} items in root.")
        for it in items[:5]:
            print(f" - {it['name']} ({'Folder' if it['is_folder'] else 'File'})")
    except Exception as e:
        print(f"Failed: {e}")
        import traceback

        traceback.print_exc()


if __name__ == "__main__":
    test_drive()
