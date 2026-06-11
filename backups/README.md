# Pulsar Backup Tool (Standalone)

This directory contains standalone scripts for backing up and restoring the Pulsar system.
It can be used independently of the main repository.

## Installation

Ensure you have `uv` installed. To install dependencies:
```bash
uv sync
```

## Configuration

1. Copy `.env.example` to `.env`.
2. Fill in your S3 credentials and Manticore URL.
3. If this folder is NOT inside the Pulsar project root, specify absolute paths to `DATA_DIR` and `STORAGE_DIR` in the `.env` file.

## Usage

### Backup
```bash
uv run backup.py
```
This will create a local archive in the `backups/` folder and upload it to S3.

### Restore
```bash
uv run restore.py
```
This is an interactive script that lets you choose from local or S3 backups.
