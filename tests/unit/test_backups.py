import sys
from unittest.mock import MagicMock

# Mock boto3 before importing the backup script
sys.modules["boto3"] = MagicMock()
sys.modules["botocore"] = MagicMock()
sys.modules["botocore.exceptions"] = MagicMock()

from pathlib import Path

from backups.backup import backup_sqlite, check_disk_space, upload_to_s3


def test_check_disk_space_success(mocker):
    # Mock disk_usage
    mock_usage = MagicMock()
    mock_usage.free = 1000 * 1024 * 1024  # 1GB
    mocker.patch("shutil.disk_usage", return_value=mock_usage)

    # Mock DB_PATH and STORAGE_DIR to control reported size
    mock_db = MagicMock()
    mock_db.exists.return_value = True
    mock_db.stat.return_value.st_size = 50 * 1024 * 1024
    mocker.patch("backups.backup.DB_PATH", mock_db)

    mock_storage = MagicMock()
    mock_storage.glob.return_value = []
    mocker.patch("backups.backup.STORAGE_DIR", mock_storage)

    assert check_disk_space() is True


def test_check_disk_space_fail(mocker):
    mock_usage = MagicMock()
    mock_usage.free = 10 * 1024 * 1024  # 10MB
    mocker.patch("shutil.disk_usage", return_value=mock_usage)

    # Mock data size (total 100MB, needs 200MB)
    mock_db = MagicMock()
    mock_db.exists.return_value = True
    mock_db.stat.return_value.st_size = 100 * 1024 * 1024
    mocker.patch("backups.backup.DB_PATH", mock_db)

    mock_storage = MagicMock()
    mock_storage.glob.return_value = []
    mocker.patch("backups.backup.STORAGE_DIR", mock_storage)

    assert check_disk_space() is False


def test_backup_sqlite(tmp_path, mocker):
    source_db = tmp_path / "source.db"
    source_db.write_text("fake db content")
    mocker.patch("backups.backup.DB_PATH", source_db)

    dest_dir = tmp_path / "dest"
    dest_dir.mkdir()

    # Mock sqlite3.connect().backup()
    mock_conn = mocker.patch("sqlite3.connect")

    backup_sqlite(dest_dir)

    assert mock_conn.called


def test_upload_to_s3(mocker):
    # Mock S3 client explicitly
    mock_s3 = MagicMock()
    mocker.patch("backups.backup.get_s3_client", return_value=mock_s3)

    fake_file = MagicMock(spec=Path)
    fake_file.name = "backup.tar.gz"
    fake_file.stat.return_value.st_size = 100

    assert upload_to_s3(fake_file) is True
    assert mock_s3.upload_file.called
