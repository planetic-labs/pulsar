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
    def mock_run_impl(args, **kwargs):
        mock_res = MagicMock()
        mock_res.returncode = 0
        if any("check_integrity" in str(arg) for arg in args):
            mock_res.stdout = "INTEGRITY_ISSUES:[]"
        else:
            mock_res.stdout = "Success/Version"
        return mock_res

    mock_run = mocker.patch("subprocess.run", side_effect=mock_run_impl)
    mock_alert = mocker.patch("cron.run_all.send_telegram_alert")

    cron_main()

    assert mock_run.called
    called_cmds = [call[0][0] for call in mock_run.call_args_list]
    assert any("backup.py" in str(cmd) for cmd in called_cmds)
    assert any("sync_index.py" in str(cmd) for cmd in called_cmds)
    assert any("check_integrity" in str(cmd) for cmd in called_cmds)
    assert not mock_alert.called


def test_cron_main_with_integrity_issues(mocker):
    def mock_run_impl(args, **kwargs):
        mock_res = MagicMock()
        mock_res.returncode = 0
        if any("check_integrity" in str(arg) for arg in args):
            mock_res.stdout = 'INTEGRITY_ISSUES:["DB mismatch", "Missing audio"]'
        else:
            mock_res.stdout = "Success/Version"
        return mock_res

    mock_run = mocker.patch("subprocess.run", side_effect=mock_run_impl)
    mock_alert = mocker.patch("cron.run_all.send_telegram_alert")

    cron_main()

    assert mock_run.called
    mock_alert.assert_called_once_with(["DB mismatch", "Missing audio"])


def test_cron_main_subprocess_failure(mocker):
    def mock_run_impl(args, **kwargs):
        if any("backup.py" in str(arg) for arg in args):
            raise subprocess.CalledProcessError(returncode=1, cmd=args, output="Failed backup", stderr="Out of space")

        mock_res = MagicMock()
        mock_res.returncode = 0
        if any("check_integrity" in str(arg) for arg in args):
            mock_res.stdout = "INTEGRITY_ISSUES:[]"
        else:
            mock_res.stdout = "Success"
        return mock_res

    mock_run = mocker.patch("subprocess.run", side_effect=mock_run_impl)
    mock_alert = mocker.patch("cron.run_all.send_telegram_alert")

    # cron_main should complete without throwing exception
    cron_main()

    assert mock_run.called
    assert not mock_alert.called
