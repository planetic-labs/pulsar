from unittest.mock import MagicMock

from app.manticore import init_manticore


def test_init_manticore_creates_tables(mocker):
    # Mock settings
    mock_q_settings = MagicMock()
    mock_q_settings.table_name = "test_chunks"
    mocker.patch("app.manticore.get_manticore_settings", return_value=mock_q_settings)

    # Mock client
    mock_client = MagicMock()
    mocker.patch("app.manticore.get_manticore_client", return_value=mock_client)

    init_manticore()

    # Check if _execute_ddl was called to create test_chunks table
    assert any("test_chunks" in call.args[0] for call in mock_client._execute_ddl.call_args_list)
