import pytest
import subprocess
from pathlib import Path
from unittest.mock import MagicMock
from app.audio import extract_audio

def test_extract_audio_success(tmp_path, mocker):
    input_video = tmp_path / "input.mp4"
    input_video.write_text("fake video")
    output_audio = tmp_path / "output.wav"
    
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = MagicMock(returncode=0)
    
    result = extract_audio(input_video, output_audio)
    
    assert result == output_audio
    assert mock_run.called
    args = mock_run.call_args[0][0]
    assert "ffmpeg" in args
    assert str(input_video) in args
    assert str(output_audio) in args

def test_extract_audio_file_not_found():
    with pytest.raises(FileNotFoundError):
        extract_audio(Path("missing.mp4"), Path("out.wav"))

def test_extract_audio_fail(tmp_path, mocker):
    input_video = tmp_path / "input.mp4"
    input_video.write_text("fake video")
    output_audio = tmp_path / "output.wav"
    
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="ffmpeg error")
    
    with pytest.raises(RuntimeError) as exc:
        extract_audio(input_video, output_audio)
    assert "ffmpeg error" in str(exc.value)
