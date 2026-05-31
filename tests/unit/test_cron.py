import subprocess
from unittest.mock import MagicMock

from cron.run_all import main as cron_main
from cron.run_all import send_telegram_alert


def test_send_telegram_alert(monkeypatch, mocker):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "111:BBB")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "2222")

    mock_post = mocker.patch("httpx.Client.post")
    mock_post.return_value.status_code = 200

    send_telegram_alert(["Error A", "Error B"])

    assert mock_post.called
    args, kwargs = mock_post.call_args
    assert "111:BBB" in args[0]
    payload = kwargs["json"]
    assert payload["chat_id"] == "2222"
    assert "Error A" in payload["text"]
    assert "Error B" in payload["text"]


def test_cron_main_success(mocker):
    # Mock subprocess.run to simulate successful runs
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "Everything fine"

    # Mock check_integrity to return no issues
    mock_integrity = mocker.patch("scripts.check_integrity.check_integrity", return_value=[])
    mock_alert = mocker.patch("cron.run_all.send_telegram_alert")

    cron_main()

    # Verify both subprocesses were called
    assert mock_run.call_count == 2
    assert mock_integrity.called
    assert not mock_alert.called


def test_cron_main_with_integrity_issues(mocker):
    # Mock subprocess.run to simulate success
    mock_run = mocker.patch("subprocess.run")
    mock_run.return_value.returncode = 0
    mock_run.return_value.stdout = "Subprocess OK"

    mock_integrity = mocker.patch(
        "scripts.check_integrity.check_integrity", return_value=["DB mismatch", "Missing audio"]
    )
    mock_alert = mocker.patch("cron.run_all.send_telegram_alert")

    cron_main()

    assert mock_run.call_count == 2
    assert mock_integrity.called
    # Alert should be triggered with consolidated list
    mock_alert.assert_called_once_with(["DB mismatch", "Missing audio"])


def test_cron_main_subprocess_failure(mocker):
    # Mock subprocess.run to raise exception on backup but succeed on sync
    def mock_run_impl(args, **kwargs):
        if "backup.py" in args[0]:
            raise subprocess.CalledProcessError(returncode=1, cmd=args, output="Failed backup", stderr="Out of space")
        # For sync
        mock_res = MagicMock()
        mock_res.returncode = 0
        mock_res.stdout = "Sync success"
        return mock_res

    mocker.patch("subprocess.run", side_effect=mock_run_impl)
    mock_integrity = mocker.patch("scripts.check_integrity.check_integrity", return_value=[])
    mock_alert = mocker.patch("cron.run_all.send_telegram_alert")

    # cron_main should complete without throwing exception
    cron_main()

    assert mock_integrity.called
    assert not mock_alert.called
