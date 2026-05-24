from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from scripts.ingest_drive_file import download_and_extract_stage


@pytest.mark.asyncio
async def test_download_and_extract_stage_wav_only(tmp_path, monkeypatch, mocker):
    # Mock settings
    mock_app_settings = MagicMock()
    mock_app_settings.disk_space_buffer_gb = 1
    mock_app_settings.downloads_dir = tmp_path / "downloads"
    mock_app_settings.audio_dir = tmp_path / "audio"
    mock_app_settings.max_audio_size_mb = 20

    mock_drive_settings = MagicMock()

    monkeypatch.setattr("scripts.ingest_drive_file.get_app_settings", lambda: mock_app_settings)
    monkeypatch.setattr("scripts.ingest_drive_file.get_google_drive_settings", lambda: mock_drive_settings)

    # Mock disk usage
    monkeypatch.setattr("shutil.disk_usage", lambda path: (100 * 1024**3, 10 * 1024**3, 90 * 1024**3))

    # Mock GoogleDriveClient
    mock_drive_client_cls = mocker.patch("scripts.ingest_drive_file.GoogleDriveClient")
    mock_drive = mock_drive_client_cls.return_value

    mock_file_meta = MagicMock()
    mock_file_meta.name = "test_video.mp4"
    mock_file_meta.size = "100"
    mock_file_meta.mime_type = "video/mp4"
    mock_file_meta.md5_checksum = "hash123"
    mock_file_meta.parents = ["parent123"]

    mock_drive.get_file = AsyncMock(return_value=mock_file_meta)
    mock_drive.download_file = AsyncMock()

    # Mock extract_audio to create a small file (1 MB)
    def fake_extract(video_path, audio_path):
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"0" * (1 * 1024 * 1024))
        return audio_path

    monkeypatch.setattr("scripts.ingest_drive_file.extract_audio", fake_extract)

    mock_convert = MagicMock()
    monkeypatch.setattr("scripts.ingest_drive_file.convert_wav_to_ogg", mock_convert)

    result = await download_and_extract_stage("file123")

    # Verify it returns wav path and didn't call convert_wav_to_ogg
    assert result["audio_path"] == str(mock_app_settings.audio_dir / "file123.wav")
    assert Path(result["audio_path"]).exists()
    assert not (mock_app_settings.audio_dir / "file123.ogg").exists()
    assert not mock_convert.called


@pytest.mark.asyncio
async def test_download_and_extract_stage_compress_to_ogg(tmp_path, monkeypatch, mocker):
    # Mock settings
    mock_app_settings = MagicMock()
    mock_app_settings.disk_space_buffer_gb = 1
    mock_app_settings.downloads_dir = tmp_path / "downloads"
    mock_app_settings.audio_dir = tmp_path / "audio"
    mock_app_settings.max_audio_size_mb = 5  # limit 5 MB

    mock_drive_settings = MagicMock()

    monkeypatch.setattr("scripts.ingest_drive_file.get_app_settings", lambda: mock_app_settings)
    monkeypatch.setattr("scripts.ingest_drive_file.get_google_drive_settings", lambda: mock_drive_settings)

    # Mock disk usage
    monkeypatch.setattr("shutil.disk_usage", lambda path: (100 * 1024**3, 10 * 1024**3, 90 * 1024**3))

    # Mock GoogleDriveClient
    mock_drive_client_cls = mocker.patch("scripts.ingest_drive_file.GoogleDriveClient")
    mock_drive = mock_drive_client_cls.return_value

    mock_file_meta = MagicMock()
    mock_file_meta.name = "test_video.mp4"
    mock_file_meta.size = "100"
    mock_file_meta.mime_type = "video/mp4"
    mock_file_meta.md5_checksum = "hash123"
    mock_file_meta.parents = ["parent123"]

    mock_drive.get_file = AsyncMock(return_value=mock_file_meta)
    mock_drive.download_file = AsyncMock()

    # Mock extract_audio to create a large file (10 MB > 5 MB)
    def fake_extract(video_path, audio_path):
        audio_path.parent.mkdir(parents=True, exist_ok=True)
        audio_path.write_bytes(b"0" * (10 * 1024 * 1024))
        return audio_path

    monkeypatch.setattr("scripts.ingest_drive_file.extract_audio", fake_extract)

    # Mock convert_wav_to_ogg to create a dummy ogg file
    def fake_convert(wav_path, ogg_path):
        ogg_path.write_text("fake ogg")
        return ogg_path

    monkeypatch.setattr("scripts.ingest_drive_file.convert_wav_to_ogg", fake_convert)

    result = await download_and_extract_stage("file123")

    # Verify it returns ogg path, deleted WAV
    assert result["audio_path"] == str(mock_app_settings.audio_dir / "file123.ogg")
    assert Path(result["audio_path"]).exists()
    assert not (mock_app_settings.audio_dir / "file123.wav").exists()
