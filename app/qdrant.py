import logging
import os
import socket
from qdrant_client import QdrantClient, models
from app.config import get_qdrant_settings, get_embedding_settings

logger = logging.getLogger(__name__)

_client = None
_sparse_model = None

def get_qdrant_client() -> QdrantClient:
    global _client
    if _client is None:
        settings = get_qdrant_settings()
        url = settings.url
        
        # Check if running outside Docker and 'qdrant' host is not reachable
        if "qdrant" in url and not os.getenv("DOCKER_CONTAINER"):
            try:
                socket.gethostbyname("qdrant")
            except socket.gaierror:
                logger.info("Host 'qdrant' not found, falling back to localhost:6333")
                url = url.replace("qdrant", "localhost")
        
        _client = QdrantClient(url=url)
    return _client

def get_sparse_embedding_model():
    global _sparse_model
    if _sparse_model is None:
        from fastembed import SparseTextEmbedding
        # prithvida/fastembed-bm25 is standard and supports Russian well
        logger.info("Initializing FastEmbed BM25 (sparse) model...")
        _sparse_model = SparseTextEmbedding(model_name="prithvida/fastembed-bm25")
    return _sparse_model

def init_qdrant():
    client = get_qdrant_client()
    settings = get_qdrant_settings()
    collection_name = settings.collection_name
    
    # Get dimension from embedding settings (e.g. 1024 for BGE-M3)
    emb_settings = get_embedding_settings()
    vector_size = emb_settings.dimension

    try:
        collections = client.get_collections().collections
        exists = any(c.name == collection_name for c in collections)
    except Exception as e:
        logger.error(f"Failed to connect to Qdrant: {e}")
        raise e

    if not exists:
        logger.info(f"Creating Qdrant collection: {collection_name} (size={vector_size})")
        client.create_collection(
            collection_name=collection_name,
            vectors_config={
                "default": models.VectorParams(
                    size=vector_size, 
                    distance=models.Distance.COSINE
                )
            },
            sparse_vectors_config={
                "text-sparse": models.SparseVectorParams(
                    index=models.SparseIndexParams(
                        on_disk=True
                    )
                )
            }
        )
        # Create payload indexes
        client.create_payload_index(collection_name=collection_name, field_name="video_id", field_schema=models.PayloadSchemaType.INTEGER)
    
    # Initialize Speaker Registry
    if not any(c.name == "speaker_registry" for c in client.get_collections().collections):
        logger.info("Creating Qdrant collection: speaker_registry")
        client.create_collection(
            collection_name="speaker_registry",
            vectors_config=models.VectorParams(
                size=192, # ECAPA-TDNN embedding size
                distance=models.Distance.COSINE
            )
        )
