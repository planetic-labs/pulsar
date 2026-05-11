from unittest.mock import MagicMock

from app.qdrant import init_qdrant


def test_init_qdrant_creates_missing_collection(mocker):
    # Mock settings
    mock_q_settings = MagicMock()
    mock_q_settings.collection_name = "test_chunks"
    mocker.patch("app.qdrant.get_qdrant_settings", return_value=mock_q_settings)

    mock_e_settings = MagicMock()
    mock_e_settings.dimension = 1024
    mocker.patch("app.qdrant.get_embedding_settings", return_value=mock_e_settings)

    # Mock client
    mock_client = MagicMock()
    # Initial state: no collections
    mock_client.get_collections.return_value = MagicMock(collections=[])
    mocker.patch("app.qdrant.get_qdrant_client", return_value=mock_client)

    init_qdrant()

    # Check if create_collection was called for chunks
    assert any(
        call.kwargs.get("collection_name") == "test_chunks" for call in mock_client.create_collection.call_args_list
    )
    # Check if create_collection was called for speaker_registry
    assert any(
        call.kwargs.get("collection_name") == "speaker_registry"
        for call in mock_client.create_collection.call_args_list
    )


def test_init_qdrant_skips_existing_collection(mocker):
    mock_q_settings = MagicMock()
    mock_q_settings.collection_name = "existing_chunks"
    mocker.patch("app.qdrant.get_qdrant_settings", return_value=mock_q_settings)
    mocker.patch("app.qdrant.get_embedding_settings", return_value=MagicMock(dimension=1024))

    mock_client = MagicMock()
    # collections already exist
    c1 = MagicMock()
    c1.name = "existing_chunks"
    c2 = MagicMock()
    c2.name = "speaker_registry"
    mock_client.get_collections.return_value = MagicMock(collections=[c1, c2])
    mocker.patch("app.qdrant.get_qdrant_client", return_value=mock_client)

    init_qdrant()

    # create_collection should NOT be called
    assert not mock_client.create_collection.called
