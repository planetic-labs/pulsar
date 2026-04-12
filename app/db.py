from __future__ import annotations

import logging
from contextlib import contextmanager
from typing import Generator

import psycopg
from psycopg.rows import dict_row

from app.config import PostgresSettings

logger = logging.getLogger(__name__)

@contextmanager
def db_connection(settings: PostgresSettings) -> Generator[psycopg.Connection, None, None]:
    """Provide a transactional scope around a series of operations."""
    conn = psycopg.connect(settings.url, row_factory=dict_row)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

def init_db(connection: psycopg.Connection) -> None:
    """Initialize the database schema, extensions and indexes."""
    # 0. Fix logs by creating postgres role if it doesn't exist (prevents FATAL auth errors from monitors)
    try:
        connection.execute("DO $$ BEGIN IF NOT EXISTS (SELECT FROM pg_catalog.pg_roles WHERE rolname = 'postgres') THEN CREATE ROLE postgres WITH LOGIN PASSWORD 'password' SUPERUSER; END IF; END $$;")
    except Exception:
        pass

    # 1. Enable pgvector extension
    connection.execute("CREATE EXTENSION IF NOT EXISTS vector")
    
    # 2. Videos table
    connection.execute("""
        CREATE TABLE IF NOT EXISTS videos (
            id SERIAL PRIMARY KEY,
            source_type TEXT NOT NULL,
            source_file_id TEXT NOT NULL,
            title TEXT NOT NULL,
            source_url TEXT,
            mime_type TEXT,
            size_bytes BIGINT,
            duration_sec DOUBLE PRECISION,
            local_video_path TEXT,
            local_audio_path TEXT,
            processing_status TEXT NOT NULL,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (source_type, source_file_id)
        )
    """)
    # Migration: add unique constraint if it doesn't exist
    has_constraint = connection.execute("""
        SELECT 1 FROM pg_constraint WHERE conname = 'videos_source_unique'
    """).fetchone()
    
    if not has_constraint:
        try:
            connection.execute("ALTER TABLE videos ADD CONSTRAINT videos_source_unique UNIQUE (source_type, source_file_id)")
        except Exception as e:
            logger.warning(f"Could not add unique constraint: {e}")

    # 3. Transcripts table
    connection.execute("""
        CREATE TABLE IF NOT EXISTS transcripts (
            id SERIAL PRIMARY KEY,
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            engine TEXT NOT NULL,
            language TEXT NOT NULL,
            transcript_text TEXT NOT NULL,
            confidence DOUBLE PRECISION,
            raw_json_path TEXT,
            normalized_json_path TEXT,
            is_primary BOOLEAN DEFAULT FALSE,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        )
    """)
    # Migration: add column if it doesn't exist
    connection.execute("ALTER TABLE transcripts ADD COLUMN IF NOT EXISTS is_primary BOOLEAN DEFAULT FALSE")

    # 4. Chunks table with Vector and FTS support
    # Note: Dimension 768 is for Google 'text-embedding-004'. 
    connection.execute("""
        CREATE TABLE IF NOT EXISTS chunks (
            id SERIAL PRIMARY KEY,
            video_id INTEGER NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
            transcript_id INTEGER NOT NULL REFERENCES transcripts(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            start_sec DOUBLE PRECISION NOT NULL,
            end_sec DOUBLE PRECISION NOT NULL,
            text TEXT NOT NULL,
            embedding vector(768),
            fts_tokens tsvector GENERATED ALWAYS AS (to_tsvector('russian', text)) STORED
        )
    """)

    # 5. Advanced Indexes
    # HNSW index for fast vector similarity search
    connection.execute("""
        CREATE INDEX IF NOT EXISTS idx_chunks_embedding_hnsw 
        ON chunks USING hnsw (embedding vector_cosine_ops)
    """)
    
    # GIN index for fast full-text search
    connection.execute("CREATE INDEX IF NOT EXISTS idx_chunks_fts ON chunks USING gin(fts_tokens)")
    
    connection.commit()
    logger.info("Database initialized with pgvector and FTS indexes.")
