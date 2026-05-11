from unittest.mock import AsyncMock, MagicMock


def test_root_redirect_to_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 307
    assert response.headers["location"].endswith("/login")


def test_login_success(client):
    response = client.post("/login", data={"token": "test-token"}, follow_redirects=False)
    assert response.status_code == 303
    assert "session" in response.cookies


def test_login_fail(client):
    response = client.post("/login", data={"token": "wrong"}, follow_redirects=False)
    assert response.status_code == 200
    assert "Invalid access token" in response.text


def test_api_drive_ls_unauthorized(client):
    response = client.get("/api/drive/ls?folder_id=root")
    assert response.status_code == 401


def test_api_drive_ls_authorized(client, mocker):
    client.post("/login", data={"token": "test-token"})

    mock_instance = MagicMock()
    mock_instance.list_folder_contents = AsyncMock(return_value=[])
    mock_instance.get_shared_drives = AsyncMock(return_value=[])

    mocker.patch("app.main.GoogleDriveClient", return_value=mock_instance)

    response = client.get("/api/drive/ls?folder_id=root")
    assert response.status_code == 200
    assert response.json() == []


def test_api_tasks_ingest(client, mocker):
    client.post("/login", data={"token": "test-token"})
    mocker.patch("app.main.get_worker")

    response = client.post("/api/tasks/ingest", data={"file_id": "file123", "title": "My Video", "diarize": "true"})
    assert response.status_code == 200
    assert response.json()["status"] == "queued"


def test_search_on_root(client, mocker, mock_qdrant):
    client.post("/login", data={"token": "test-token"})
    mock_hybrid = mocker.patch("app.main.hybrid_search", new_callable=AsyncMock)
    mock_hybrid.return_value = []

    response = client.get("/?q=test")
    assert response.status_code == 200
    assert "test" in response.text
