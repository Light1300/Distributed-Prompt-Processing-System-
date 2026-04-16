import hashlib
import numpy as np
from src.core.config import get_settings
from src.core.logging import get_logger

logger = get_logger(__name__)
settings = get_settings()

EMBEDDING_DIM = 384


class SentenceTransformerEmbedder:
    def __init__(self) -> None:
        logger.info("loading_embedding_model", model=settings.embedding_model)
        from sentence_transformers import SentenceTransformer
        self._model = SentenceTransformer(settings.embedding_model)
        logger.info("embedding_model_loaded")

    def encode(self, text: str) -> np.ndarray:
        vector = self._model.encode(text, normalize_embeddings=True)
        return np.array(vector, dtype=np.float32)

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        return float(np.dot(a, b))


class MockEmbedder:
    def encode(self, text: str) -> np.ndarray:
        digest = hashlib.sha256(text.encode()).digest()
        rng = np.random.default_rng(list(digest[:8]))
        vec = rng.standard_normal(EMBEDDING_DIM).astype(np.float32)
        norm = np.linalg.norm(vec)
        return vec / norm if norm > 0 else vec

    def cosine_similarity(self, a: np.ndarray, b: np.ndarray) -> float:
        return float(np.dot(a, b))


_embedder = None


def get_embedder():
    global _embedder
    if _embedder is None:
        if settings.use_mock_embeddings:
            _embedder = MockEmbedder()
        else:
            _embedder = SentenceTransformerEmbedder()
    return _embedder