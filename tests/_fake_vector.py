"""Test doubles for sentence-transformers / FAISS to keep tests offline."""

from __future__ import annotations

import hashlib
import math
import pickle
from dataclasses import dataclass
from typing import Sequence

import numpy as np


KEYWORDS: tuple[str, ...] = (
    "refresh",
    "token",
    "rotation",
    "access",
    "セッション",
    "延長",
    "ログイン",
    "状態",
    "認証",
)


def _vector_for_text(text: str) -> np.ndarray:
    """Map a text to a deterministic vector by counting keyword occurrences."""

    lowered = text.lower()
    counts = np.array(
        [float(lowered.count(keyword.lower())) for keyword in KEYWORDS],
        dtype="float32",
    )
    if counts.sum() == 0.0:
        # Add a tiny salt so unrelated texts still get distinct vectors.
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        counts = np.array(
            [float(b) / 255.0 for b in digest[: len(KEYWORDS)]],
            dtype="float32",
        )
    return counts


def _normalize(vec: np.ndarray) -> np.ndarray:
    norm = float(np.linalg.norm(vec))
    if norm == 0.0:
        return vec
    return vec / norm


class FakeEmbedder:
    """Deterministic embedder used in tests."""

    def encode(self, texts: Sequence[str], *, batch_size: int, normalize: bool):
        del batch_size
        vectors = [_vector_for_text(text) for text in texts]
        if normalize:
            vectors = [_normalize(vec) for vec in vectors]
        return np.asarray(vectors, dtype="float32")


def fake_embedder_factory(_model_name: str) -> FakeEmbedder:
    return FakeEmbedder()


@dataclass
class _FakeIndex:
    dim: int
    vectors: np.ndarray  # shape: (n, dim)

    def add(self, embeddings: np.ndarray) -> None:
        if embeddings.shape[1] != self.dim:
            raise ValueError("dim mismatch in fake index add")
        self.vectors = np.vstack([self.vectors, embeddings.astype("float32")])

    def search(self, query: np.ndarray, top_k: int):
        if self.vectors.shape[0] == 0:
            scores = np.full((query.shape[0], top_k), -math.inf, dtype="float32")
            ids = np.full((query.shape[0], top_k), -1, dtype="int64")
            return scores, ids
        sims = query @ self.vectors.T
        order = np.argsort(-sims, axis=1)[:, :top_k]
        rows = np.arange(sims.shape[0])[:, None]
        scores = sims[rows, order]
        if order.shape[1] < top_k:
            pad = top_k - order.shape[1]
            scores = np.pad(scores, ((0, 0), (0, pad)), constant_values=-math.inf)
            order = np.pad(order, ((0, 0), (0, pad)), constant_values=-1)
        return scores.astype("float32"), order.astype("int64")


class FakeFaissModule:
    """Drop-in replacement for the `faiss` module used by tools.vector_index."""

    @staticmethod
    def IndexFlatIP(dim: int) -> _FakeIndex:
        return _FakeIndex(dim=dim, vectors=np.zeros((0, dim), dtype="float32"))

    @staticmethod
    def write_index(index: _FakeIndex, path: str) -> None:
        with open(path, "wb") as fh:
            pickle.dump({"dim": index.dim, "vectors": index.vectors}, fh)

    @staticmethod
    def read_index(path: str) -> _FakeIndex:
        with open(path, "rb") as fh:
            payload = pickle.load(fh)
        return _FakeIndex(dim=payload["dim"], vectors=payload["vectors"])
