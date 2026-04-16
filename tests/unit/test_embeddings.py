import numpy as np
import pytest

from src.services.embeddings import MockEmbedder, EMBEDDING_DIM


@pytest.fixture
def embedder():
    return MockEmbedder()


class TestMockEmbedder:
    def test_output_dimension(self, embedder):
        vec = embedder.encode("test text")
        assert vec.shape == (EMBEDDING_DIM,)

    def test_output_is_float32(self, embedder):
        vec = embedder.encode("test text")
        assert vec.dtype == np.float32

    def test_deterministic_same_input(self, embedder):
        v1 = embedder.encode("explain machine learning")
        v2 = embedder.encode("explain machine learning")
        np.testing.assert_array_equal(v1, v2)

    def test_different_inputs_different_vectors(self, embedder):
        v1 = embedder.encode("cats are fluffy")
        v2 = embedder.encode("the economy is growing")
        assert not np.allclose(v1, v2)

    def test_vector_is_l2_normalized(self, embedder):
        vec = embedder.encode("any text here")
        norm = float(np.linalg.norm(vec))
        assert abs(norm - 1.0) < 1e-5, f"Expected unit norm, got {norm}"

    def test_cosine_similarity_identical_vectors(self, embedder):
        vec = embedder.encode("identical text")
        sim = embedder.cosine_similarity(vec, vec)
        assert abs(sim - 1.0) < 1e-5

    def test_cosine_similarity_bounds(self, embedder):
        texts = ["cat", "quantum physics", "stock market", "python programming"]
        vectors = [embedder.encode(t) for t in texts]
        for i, v1 in enumerate(vectors):
            for j, v2 in enumerate(vectors):
                sim = embedder.cosine_similarity(v1, v2)
                assert -1.01 <= sim <= 1.01

    def test_empty_string_does_not_crash(self, embedder):
        vec = embedder.encode("")
        assert vec.shape == (EMBEDDING_DIM,)