# VideoDB Backup Tool (Standalone)

This directory contains standalone scripts for backing up and restoring the VideoDB system.
It can be used independently of the main repository.

## Installation

Ensure you have the required dependencies:
```bash
pip install boto3 httpx python-dotenv
```

## Configuration

1. Copy `.env.example` to `.env`.
2. Fill in your S3 credentials and Qdrant URL.
3. If this folder is NOT inside the VideoDB project root, specify absolute paths to `DATA_DIR` and `STORAGE_DIR` in the `.env` file.

## Usage

### Backup
```bash
python backup.py
```
This will create a local archive in the `backups/` folder and upload it to S3.

### Restore
```bash
python restore.py
```
This is an interactive script that lets you choose from local or S3 backups.
