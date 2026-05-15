# Installation and Setup Guide

This guide will help you set up VideoDB AI from scratch using Docker.

## Prerequisites

- **Docker** and **Docker Compose** installed.
- A **Deepgram API Key** for speech-to-text.
- A **Google Cloud Service Account** with "Viewer" access to the target Google Drive folders.
- An **Infinity Embedding Service** instance (or any OpenAI-compatible embedding API).

## Step 1: Clone the Repository

```bash
git clone https://github.com/your-username/video-db.git
cd video-db
```

## Step 2: Configure Environment Variables

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

### Key Variables to Set:
- `APP_ACCESS_TOKEN`: Your password for the UI.
- `SESSION_SECRET_KEY`: A long random string for session encryption.
- `DEEPGRAM_API_KEY`: Your key from Deepgram.
- `EMBEDDING_API_URL`: The URL of your Infinity embedding service.
- `GOOGLE_DRIVE_CREDENTIALS_PATH`: Usually `/srv/search-ui/config/service-key.json` inside the container.

## Step 3: Google Drive Credentials

1.  Go to the [Google Cloud Console](https://console.cloud.google.com/).
2.  Create a **Service Account** and download its JSON key file.
3.  Place this file in the project directory as `config/service-key.json`.
4.  **Important**: Share your Google Drive folders/disks with the email address of the service account.

## Step 4: Launch with Docker

```bash
docker compose up -d --build
```

The application will be available at `http://localhost:8000`.

## Step 5: Verify the Setup

1.  Login using your `APP_ACCESS_TOKEN`.
2.  Go to the **Import** tab.
3.  You should see your Google Drive folders.
4.  Select a small video and click "Import" to test the pipeline.
5.  Monitor progress in the **Status** tab.

## Troubleshooting

- **Disk Space**: The system requires a buffer (default 3GB) to process videos. Ensure your host machine has enough space.
- **Network**: Ensure the container can reach `api.deepgram.com` and your embedding service.
- **Logs**: View real-time logs in the container:
  ```bash
  docker compose logs -f app
  ```
