from pathlib import Path
from unittest.mock import MagicMock

from backups.restore import check_disk_space, manage_app_container


def test_check_disk_space_restore(mocker):
    mock_usage = MagicMock()
    mock_usage.free = 1000
    mocker.patch("shutil.disk_usage", return_value=mock_usage)

    # 1. File exists
    fake_archive = MagicMock(spec=Path)
    fake_archive.exists.return_value = True
    fake_archive.stat.return_value.st_size = 100  # needs 500

    mock_usage.free = 600
    assert check_disk_space(fake_archive) is True

    mock_usage.free = 400
    assert check_disk_space(fake_archive) is False


def test_manage_app_container(mocker):
    mock_run = mocker.patch("subprocess.run")
    manage_app_container("stop")
    assert mock_run.called
    args = mock_run.call_args[0][0]
    assert "docker" in args
    assert "stop" in args
    assert "app" in args
