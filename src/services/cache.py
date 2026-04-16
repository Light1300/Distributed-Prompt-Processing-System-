import json
from datetime import datetime, timezone

import numpy as np
from sqlalchemy import select, update, func
from sqlalchemy.orm import Session

from src.core.config import get_settings
from src.core.logging import get_logger
from src.models.db import SemanticCache
from src.services.embeddings import get_embedder

logger = get_logger(__name__)
settings = get_settings()


class CacheService:
    def __init__(self, db: Session) -> None:
        self._db = db
        self._embedder = get_embedder()
        self._threshold = settings.cache_similarity_threshold

    def lookup(self, text: str) -> tuple[str, float] | None:
        query_vec = self._embedder.encode(text)
        rows = self._db.execute(
            select(SemanticCache.id, SemanticCache.embedding_json, SemanticCache.response)
        ).fetchall()

        if not rows:
            return None

        best_score = -1.0
        best_row = None
        for row in rows:
            cached_vec = np.array(json.loads(row.embedding_json), dtype=np.float32)
            score = self._embedder.cosine_similarity(query_vec, cached_vec)
            if score > best_score:
                best_score = score
                best_row = row

        if best_score >= self._threshold and best_row is not None:
            self._db.execute(
                update(SemanticCache)
                .where(SemanticCache.id == best_row.id)
                .values(
                    hit_count=SemanticCache.hit_count + 1,
                    last_hit_at=datetime.now(timezone.utc),
                )
            )
            self._db.commit()
            logger.info("cache_hit", similarity=round(best_score, 4))
            return best_row.response, best_score

        logger.info("cache_miss", best_similarity=round(best_score, 4))
        return None

    def store(self, text: str, response: str) -> None:
        embedding = self._embedder.encode(text)
        entry = SemanticCache(
            prompt_text=text,
            embedding_json=json.dumps(embedding.tolist()),
            response=response,
        )
        self._db.add(entry)
        self._db.commit()

    def total_entries(self) -> int:
        return self._db.execute(
            select(func.count()).select_from(SemanticCache)
        ).scalar() or 0