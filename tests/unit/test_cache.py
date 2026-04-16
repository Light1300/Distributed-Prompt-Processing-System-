import json
from unittest.mock import patch

import numpy as np
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.core.database import Base
from src.models.db import SemanticCache
from src.services.cache import CacheService
from src.services.embeddings import MockEmbedder


@pytest.fixture(scope="function")
def db_session():
    engine = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(engine)


@pytest.fixture
def embedder():
    return MockEmbedder()


@pytest.fixture
def cache(db_session, embedder):
    with patch("src.services.cache.get_embedder", return_value=embedder):
        service = CacheService(db_session)
    return service


def _insert_entry(db_session, embedder, text: str, response: str):
    vec = embedder.encode(text)
    entry = SemanticCache(
        prompt_text=text,
        embedding_json=json.dumps(vec.tolist()),
        response=response,
    )
    db_session.add(entry)
    db_session.commit()


class TestCacheLookup:
    def test_exact_match_returns_response(self, cache, db_session, embedder):
        _insert_entry(db_session, embedder, "explain quantum computing", "Quantum response")
        result = cache.lookup("explain quantum computing")
        assert result is not None
        response, score = result
        assert response == "Quantum response"
        assert score >= 0.99

    def test_empty_cache_returns_none(self, cache):
        assert cache.lookup("anything") is None

    def test_below_threshold_returns_none(self, cache, db_session, embedder):
        _insert_entry(db_session, embedder, "cat pictures", "Cats response")
        cache._threshold = 0.9
        result = cache.lookup("quantum physics thermodynamics nuclear")
        assert result is None

    def test_hit_increments_hit_count(self, cache, db_session, embedder):
        _insert_entry(db_session, embedder, "what is python", "Python response")
        cache.lookup("what is python")
        cache.lookup("what is python")
        entry = db_session.query(SemanticCache).first()
        assert entry.hit_count == 2

    def test_hit_updates_last_hit_at(self, cache, db_session, embedder):
        _insert_entry(db_session, embedder, "hello world", "Hello response")
        entry_before = db_session.query(SemanticCache).first()
        assert entry_before.last_hit_at is None
        cache.lookup("hello world")
        db_session.expire_all()
        entry_after = db_session.query(SemanticCache).first()
        assert entry_after.last_hit_at is not None


class TestCacheStore:
    def test_store_creates_entry(self, cache, db_session):
        cache.store("new prompt", "new response")
        entries = db_session.query(SemanticCache).all()
        assert len(entries) == 1
        assert entries[0].prompt_text == "new prompt"
        assert entries[0].response == "new response"
        assert entries[0].hit_count == 0

    def test_store_then_lookup_finds_entry(self, cache):
        cache.store("machine learning basics", "ML response")
        result = cache.lookup("machine learning basics")
        assert result is not None
        assert result[0] == "ML response"

    def test_total_entries_count(self, cache):
        assert cache.total_entries() == 0
        cache.store("prompt 1", "response 1")
        cache.store("prompt 2", "response 2")
        assert cache.total_entries() == 2


class TestSimilarityThreshold:
    def test_impossible_threshold_never_hits(self, cache, db_session, embedder):
        _insert_entry(db_session, embedder, "same text here", "response")
        cache._threshold = 1.1
        result = cache.lookup("same text here")
        assert result is None

    def test_negative_threshold_always_hits(self, cache, db_session, embedder):
        _insert_entry(db_session, embedder, "text A", "response A")
        cache._threshold = -1.0
        result = cache.lookup("completely different text xyz")
        assert result is not None