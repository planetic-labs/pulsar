import logging
from qdrant_client import QdrantClient, models
from fastembed import SparseTextEmbedding
from app.config import get_qdrant_settings

logger = logging.getLogger(__name__)

_client = None
_sparse_model = None

def get_qdrant_client() -> QdrantClient:
    global _client
    if _client is None:
        settings = get_qdrant_settings()
        _client = QdrantClient(url=settings.url)
    return _client

def get_sparse_embedding_model() -> SparseTextEmbedding:
    global _sparse_model
    if _sparse_model is None:
        # BM25 model for sparse vectors
        _sparse_model = SparseTextEmbedding(model_name="Qdrant/bm25")
    return _sparse_model

def init_qdrant():
    client = get_qdrant_client()
    settings = get_qdrant_settings()
    collection_name = settings.collection_name

    collections = client.get_collections().collections
    exists = any(c.name == collection_name for c in collections)

    if not exists:
        logger.info(f"Creating Qdrant collection: {collection_name}")
        client.create_collection(
            collection_name=collection_name,
            vectors_config={
                "default": models.VectorParams(
                    size=768, 
                    distance=models.Distance.COSINE
                )
            },
            sparse_vectors_config={
                "text-sparse": models.SparseVectorParams(
                    modifier=models.Modifier.IDF
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
