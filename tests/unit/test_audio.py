from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.audio import convert_wav_to_ogg, extract_audio


def test_extract_audio_success(tmp_path, mocker):
    input_video = tmp_path / "input.mp4"
    input_video.write_text("fake video")
    output_audio = tmp_path / "output.wav"

    mocker.patch("app.audio._has_audio_stream", return_value=True)
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

    mocker.patch("app.audio._has_audio_stream", return_value=True)
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="ffmpeg error")

    with pytest.raises(RuntimeError) as exc:
        extract_audio(input_video, output_audio)
    assert "ffmpeg error" in str(exc.value)


def test_convert_wav_to_ogg_success(tmp_path, mocker):
    input_wav = tmp_path / "input.wav"
    input_wav.write_text("fake wav")
    output_ogg = tmp_path / "output.ogg"

    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = MagicMock(returncode=0)

    result = convert_wav_to_ogg(input_wav, output_ogg)

    assert result == output_ogg
    assert mock_run.called
    args = mock_run.call_args[0][0]
    assert "ffmpeg" in args
    assert "libvorbis" in args
    assert str(input_wav) in args
    assert str(output_ogg) in args


def test_convert_wav_to_ogg_file_not_found():
    with pytest.raises(FileNotFoundError):
        convert_wav_to_ogg(Path("missing.wav"), Path("out.ogg"))


def test_convert_wav_to_ogg_fail(tmp_path, mocker):
    input_wav = tmp_path / "input.wav"
    input_wav.write_text("fake wav")
    output_ogg = tmp_path / "output.ogg"

    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="ffmpeg error")

    with pytest.raises(RuntimeError) as exc:
        convert_wav_to_ogg(input_wav, output_ogg)
    assert "ffmpeg error" in str(exc.value)
