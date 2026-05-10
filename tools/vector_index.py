"""Embedding generation and FAISS vector index management."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Protocol, Sequence

from utils import (
    DEFAULT_EMBEDDING_BATCH_SIZE,
    DEFAULT_EMBEDDING_MODEL,
    DEFAULT_FAISS_INDEX_PATH,
    DEFAULT_VECTOR_META_PATH,
    atomic_write_text,
    ensure_parent_directory,
    now_iso,
)


VECTOR_BACKEND_NONE = "none"
VECTOR_BACKEND_FAISS = "faiss"
SUPPORTED_VECTOR_BACKENDS = {VECTOR_BACKEND_NONE, VECTOR_BACKEND_FAISS}

FAISS_INDEX_TYPE_FLAT_IP = "IndexFlatIP"
FAISS_METRIC_INNER_PRODUCT = "inner_product"


class VectorIndexError(RuntimeError):
    """Raised when vector index construction or lookup fails."""


class VectorBackendUnavailableError(VectorIndexError):
    """Raised when an optional vector backend dependency is missing."""


class VectorMetaMismatchError(VectorIndexError):
    """Raised when index metadata is incompatible with a query model."""


@dataclass(slots=True)
class VectorBackendConfig:
    """Runtime configuration for the vector backend."""

    backend: str = VECTOR_BACKEND_NONE
    embedding_model: str = DEFAULT_EMBEDDING_MODEL
    embedding_batch_size: int = DEFAULT_EMBEDDING_BATCH_SIZE
    normalize: bool = True
    faiss_index_path: Path = DEFAULT_FAISS_INDEX_PATH
    vector_meta_path: Path = DEFAULT_VECTOR_META_PATH


@dataclass(slots=True)
class VectorMeta:
    """Persistent metadata describing the FAISS index on disk."""

    backend: str
    embedding_model: str
    embedding_dim: int
    normalized: bool
    metric: str
    index_type: str
    created_at: str
    chunk_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "embedding_model": self.embedding_model,
            "embedding_dim": self.embedding_dim,
            "normalized": self.normalized,
            "metric": self.metric,
            "index_type": self.index_type,
            "created_at": self.created_at,
            "chunk_count": self.chunk_count,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "VectorMeta":
        return cls(
            backend=str(payload["backend"]),
            embedding_model=str(payload["embedding_model"]),
            embedding_dim=int(payload["embedding_dim"]),
            normalized=bool(payload["normalized"]),
            metric=str(payload["metric"]),
            index_type=str(payload["index_type"]),
            created_at=str(payload["created_at"]),
            chunk_count=int(payload["chunk_count"]),
        )


@dataclass(slots=True)
class VectorSearchHit:
    """A single FAISS hit returned by the backend."""

    vector_id: int
    score: float


class Embedder(Protocol):
    """Protocol implemented by embedding model wrappers and test fakes."""

    def encode(
        self,
        texts: Sequence[str],
        *,
        batch_size: int,
        normalize: bool,
    ) -> Any:
        """Return a 2D float32 ndarray-like with shape (len(texts), dim)."""


def load_vector_backend_config(env: dict[str, str] | None = None) -> VectorBackendConfig:
    """Load vector backend configuration from environment variables."""

    source = env if env is not None else os.environ
    backend = source.get("DOC_VECTOR_BACKEND", VECTOR_BACKEND_NONE).strip().lower() or VECTOR_BACKEND_NONE
    if backend not in SUPPORTED_VECTOR_BACKENDS:
        raise VectorIndexError(
            f"Unsupported DOC_VECTOR_BACKEND={backend!r}. Use one of: "
            + ", ".join(sorted(SUPPORTED_VECTOR_BACKENDS))
        )

    normalize_raw = source.get("DOC_EMBEDDING_NORMALIZE", "true").strip().lower()
    normalize = normalize_raw not in {"0", "false", "no", "off"}

    batch_raw = source.get("DOC_EMBEDDING_BATCH_SIZE")
    batch_size = int(batch_raw) if batch_raw else DEFAULT_EMBEDDING_BATCH_SIZE

    return VectorBackendConfig(
        backend=backend,
        embedding_model=source.get("DOC_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
        embedding_batch_size=batch_size,
        normalize=normalize,
        faiss_index_path=Path(source.get("DOC_FAISS_INDEX_PATH", str(DEFAULT_FAISS_INDEX_PATH))),
        vector_meta_path=Path(source.get("DOC_VECTOR_META_PATH", str(DEFAULT_VECTOR_META_PATH))),
    )


def make_embedding_text(chunk: dict[str, Any]) -> str:
    """Render the canonical embedding input text for a chunk."""

    title = (chunk.get("title") or "").strip()
    headings = (chunk.get("headings") or "").strip()
    body = (chunk.get("body") or "").strip()

    labels: list[str] = []
    labels_json = chunk.get("labels_json")
    if labels_json:
        try:
            parsed = json.loads(labels_json)
        except json.JSONDecodeError:
            parsed = []
        if isinstance(parsed, list):
            labels = [str(item) for item in parsed if item]

    label_text = ", ".join(labels)
    return (
        f"Title: {title}\n"
        f"Headings: {headings}\n"
        f"Labels: {label_text}\n"
        f"\n"
        f"{body}"
    )


def chunk_content_hash(chunk: dict[str, Any]) -> str:
    """Stable hash for the embedding input text."""

    import hashlib

    digest = hashlib.sha256(make_embedding_text(chunk).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def write_vector_meta(path: Path, meta: VectorMeta) -> None:
    """Persist vector_meta.json atomically."""

    payload = json.dumps(meta.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    atomic_write_text(path, payload)


def read_vector_meta(path: Path) -> VectorMeta:
    """Load vector_meta.json from disk."""

    if not path.exists():
        raise VectorIndexError(
            f"vector_meta.json not found at {path}. ベクトルインデックスを再構築してください。"
        )
    return VectorMeta.from_dict(json.loads(path.read_text(encoding="utf-8")))


def verify_meta_compatibility(
    meta: VectorMeta,
    *,
    expected_model: str,
    expected_dim: int | None = None,
) -> None:
    """Raise if persisted meta does not match the runtime configuration."""

    if meta.embedding_model != expected_model:
        raise VectorMetaMismatchError(
            "Embedding model mismatch: index was built with "
            f"{meta.embedding_model!r}, query uses {expected_model!r}. "
            "再構築するか、設定を合わせてください。"
        )
    if expected_dim is not None and meta.embedding_dim != expected_dim:
        raise VectorMetaMismatchError(
            "Embedding dimension mismatch: index="
            f"{meta.embedding_dim}, query={expected_dim}."
        )


def load_sentence_transformer_embedder(
    model_name: str,
    *,
    sentence_transformers_module: Any | None = None,
) -> Embedder:
    """Load a sentence-transformers backed Embedder, importing lazily."""

    module = sentence_transformers_module
    if module is None:
        try:
            import sentence_transformers as module  # type: ignore[no-redef]
        except ImportError as exc:
            raise VectorBackendUnavailableError(
                "sentence-transformers がインストールされていません。"
                " `uv sync --extra vector` で追加してください。"
            ) from exc

    model = module.SentenceTransformer(model_name)
    return _SentenceTransformerEmbedder(model)


class _SentenceTransformerEmbedder:
    """Adapter around a SentenceTransformer model."""

    def __init__(self, model: Any) -> None:
        self._model = model

    def encode(
        self,
        texts: Sequence[str],
        *,
        batch_size: int,
        normalize: bool,
    ) -> Any:
        try:
            import numpy as np  # type: ignore
        except ImportError as exc:
            raise VectorBackendUnavailableError(
                "numpy がインストールされていません。"
                " `uv sync --extra vector` で追加してください。"
            ) from exc

        embeddings = self._model.encode(
            list(texts),
            batch_size=batch_size,
            normalize_embeddings=normalize,
            show_progress_bar=False,
            convert_to_numpy=True,
        )
        return np.asarray(embeddings, dtype="float32")


def _import_faiss() -> Any:
    try:
        import faiss  # type: ignore
    except ImportError as exc:
        raise VectorBackendUnavailableError(
            "faiss がインストールされていません。"
            " `uv sync --extra vector` で追加してください。"
        ) from exc
    return faiss


def _import_numpy() -> Any:
    try:
        import numpy as np  # type: ignore
    except ImportError as exc:
        raise VectorBackendUnavailableError(
            "numpy がインストールされていません。"
            " `uv sync --extra vector` で追加してください。"
        ) from exc
    return np


def build_faiss_index(
    embeddings: Any,
    *,
    faiss_module: Any | None = None,
) -> Any:
    """Build an IndexFlatIP from float32 embeddings."""

    faiss = faiss_module or _import_faiss()
    if embeddings.ndim != 2:
        raise VectorIndexError("embeddings must be 2D.")
    dim = int(embeddings.shape[1])
    index = faiss.IndexFlatIP(dim)
    index.add(embeddings)
    return index


def write_faiss_index(index: Any, path: Path, *, faiss_module: Any | None = None) -> None:
    """Persist a FAISS index atomically by writing to a sibling .tmp file."""

    faiss = faiss_module or _import_faiss()
    ensure_parent_directory(path)
    temp_path = path.with_name(f".{path.name}.tmp")
    faiss.write_index(index, str(temp_path))
    os.replace(temp_path, path)


def read_faiss_index(path: Path, *, faiss_module: Any | None = None) -> Any:
    """Load a FAISS index from disk."""

    faiss = faiss_module or _import_faiss()
    if not path.exists():
        raise VectorIndexError(
            f"FAISS index not found at {path}. ベクトルインデックスを再構築してください。"
        )
    return faiss.read_index(str(path))


def search_faiss(index: Any, query_embedding: Any, top_k: int) -> list[VectorSearchHit]:
    """Run a FAISS search and convert results to VectorSearchHit."""

    if query_embedding.ndim == 1:
        query_embedding = query_embedding.reshape(1, -1)
    scores, vector_ids = index.search(query_embedding, top_k)
    hits: list[VectorSearchHit] = []
    for score, vector_id in zip(scores[0], vector_ids[0]):
        if int(vector_id) < 0:
            continue
        hits.append(VectorSearchHit(vector_id=int(vector_id), score=float(score)))
    return hits


@dataclass(slots=True)
class FaissBuildResult:
    """Outcome of a FAISS rebuild."""

    chunk_count: int
    embedding_dim: int
    meta: VectorMeta


def build_faiss_artifacts(
    chunks: list[dict[str, Any]],
    *,
    config: VectorBackendConfig,
    embedder_factory: Callable[[str], Embedder] | None = None,
    faiss_module: Any | None = None,
    numpy_module: Any | None = None,
    now: Callable[[], str] = now_iso,
) -> tuple[FaissBuildResult, list[tuple[int, str, dict[str, Any]]]]:
    """Generate embeddings, FAISS index, and meta from chunk rows.

    Returns the build summary plus a list of (vector_id, content_hash, chunk_row)
    tuples that the caller can persist to vector_chunks.
    """

    if not chunks:
        empty_meta = VectorMeta(
            backend=VECTOR_BACKEND_FAISS,
            embedding_model=config.embedding_model,
            embedding_dim=0,
            normalized=config.normalize,
            metric=FAISS_METRIC_INNER_PRODUCT,
            index_type=FAISS_INDEX_TYPE_FLAT_IP,
            created_at=now(),
            chunk_count=0,
        )
        write_vector_meta(config.vector_meta_path, empty_meta)
        if config.faiss_index_path.exists():
            config.faiss_index_path.unlink()
        return FaissBuildResult(chunk_count=0, embedding_dim=0, meta=empty_meta), []

    factory = embedder_factory or load_sentence_transformer_embedder
    embedder = factory(config.embedding_model)
    np = numpy_module or _import_numpy()

    texts = [make_embedding_text(chunk) for chunk in chunks]
    embeddings = embedder.encode(
        texts,
        batch_size=config.embedding_batch_size,
        normalize=config.normalize,
    )
    embeddings = np.asarray(embeddings, dtype="float32")
    if embeddings.ndim != 2 or embeddings.shape[0] != len(chunks):
        raise VectorIndexError(
            "Embedder returned incompatible shape: "
            f"expected ({len(chunks)}, dim), got {tuple(embeddings.shape)}"
        )

    index = build_faiss_index(embeddings, faiss_module=faiss_module)
    write_faiss_index(index, config.faiss_index_path, faiss_module=faiss_module)

    meta = VectorMeta(
        backend=VECTOR_BACKEND_FAISS,
        embedding_model=config.embedding_model,
        embedding_dim=int(embeddings.shape[1]),
        normalized=config.normalize,
        metric=FAISS_METRIC_INNER_PRODUCT,
        index_type=FAISS_INDEX_TYPE_FLAT_IP,
        created_at=now(),
        chunk_count=int(embeddings.shape[0]),
    )
    write_vector_meta(config.vector_meta_path, meta)

    rows: list[tuple[int, str, dict[str, Any]]] = []
    for vector_id, chunk in enumerate(chunks):
        rows.append((vector_id, chunk_content_hash(chunk), chunk))
    return FaissBuildResult(chunk_count=meta.chunk_count, embedding_dim=meta.embedding_dim, meta=meta), rows


def embed_query(
    query: str,
    *,
    config: VectorBackendConfig,
    embedder_factory: Callable[[str], Embedder] | None = None,
    numpy_module: Any | None = None,
) -> Any:
    """Embed a single query string into a 1xN float32 array."""

    factory = embedder_factory or load_sentence_transformer_embedder
    embedder = factory(config.embedding_model)
    np = numpy_module or _import_numpy()
    embedding = embedder.encode(
        [query],
        batch_size=1,
        normalize=config.normalize,
    )
    return np.asarray(embedding, dtype="float32")


