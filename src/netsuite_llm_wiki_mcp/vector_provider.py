"""Optional, offline-only embedding providers for vector retrieval.

This module deliberately keeps its import surface to the Python standard
library.  ``sentence_transformers`` is imported only when the local BGE-M3
provider is instantiated for an explicit build, update, or enabled query.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, Sequence


_LOCAL_MODEL_CACHE: dict[tuple[str, str, int], tuple[Any, "VectorProviderIdentity"]] = {}
_LOCAL_MODEL_CACHE_LOCK = threading.Lock()


class VectorProviderError(RuntimeError):
    """A provider failure that can be exposed as a stable public code."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VectorProviderIdentity:
    provider_id: str
    model_id: str
    model_version: str
    dimensions: int

    def to_public_dict(self) -> dict[str, object]:
        return {
            "provider_id": self.provider_id,
            "model_id": self.model_id,
            "model_version": self.model_version,
            "dimensions": self.dimensions,
        }


class VectorProvider(Protocol):
    def identity(self) -> VectorProviderIdentity: ...

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]: ...

    def embed_query(self, text: str) -> list[float]: ...


class DeterministicFakeProvider:
    """A no-network provider for deterministic vector retrieval tests.

    ``semantic_vectors`` maps a case-insensitive phrase to a vector.  The
    first phrase contained in an input selects that vector; all other values
    receive a stable hash-derived vector.
    """

    def __init__(
        self,
        semantic_vectors: dict[str, Sequence[float]] | None = None,
        *,
        dimensions: int = 4,
        model_id: str = "deterministic-fake-v1",
    ) -> None:
        self._semantic_vectors = {
            phrase.casefold(): _normalize([float(value) for value in vector])
            for phrase, vector in (semantic_vectors or {}).items()
        }
        self._dimensions = dimensions
        self._model_id = model_id
        if any(len(vector) != dimensions for vector in self._semantic_vectors.values()):
            raise ValueError("all fake vectors must match dimensions")

    def identity(self) -> VectorProviderIdentity:
        return VectorProviderIdentity(
            provider_id="deterministic-fake",
            model_id=self._model_id,
            model_version="1",
            dimensions=self._dimensions,
        )

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed(text) for text in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._embed(text)

    def _embed(self, text: str) -> list[float]:
        lowered = text.casefold()
        for phrase, vector in self._semantic_vectors.items():
            if phrase in lowered:
                return list(vector)
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = [((digest[index] / 255.0) * 2.0) - 1.0 for index in range(self._dimensions)]
        return _normalize(values)


class LocalBgeM3Provider:
    """Loads a BGE-M3 SentenceTransformer only from a local model directory."""

    provider_id = "local_bge_m3"

    def __init__(
        self,
        model_path: str | Path,
        *,
        device: str = "cpu",
        batch_size: int = 16,
        max_sequence_length: int = 512,
    ) -> None:
        self.model_path = Path(model_path).expanduser().resolve()
        self.device = device
        self.batch_size = batch_size
        self.max_sequence_length = max_sequence_length
        self._model: object | None = None
        self._identity: VectorProviderIdentity | None = None

    def identity(self) -> VectorProviderIdentity:
        self._load_model()
        assert self._identity is not None
        return self._identity

    def embed_documents(self, texts: Sequence[str]) -> list[list[float]]:
        if not texts:
            return []
        model: Any = self._load_model()
        encoder = getattr(model, "encode_document", None)
        if encoder is None:
            encoder = model.encode
        encoded = encoder(
            list(texts),
            batch_size=self.batch_size,
            show_progress_bar=False,
            convert_to_numpy=True,
            normalize_embeddings=True,
        )
        return [_normalize([float(value) for value in vector]) for vector in encoded.tolist()]

    def embed_query(self, text: str) -> list[float]:
        model: Any = self._load_model()
        encoder = getattr(model, "encode_query", None)
        if encoder is None:
            return self.embed_documents([text])[0]
        encoded = encoder(text, show_progress_bar=False, convert_to_numpy=True, normalize_embeddings=True)
        values = encoded.tolist() if hasattr(encoded, "tolist") else encoded
        return _normalize([float(value) for value in values])

    def _load_model(self):
        if self._model is not None:
            return self._model
        if not self.model_path.is_dir():
            raise VectorProviderError("model_missing", "the configured local embedding model is unavailable")
        if importlib.util.find_spec("sentence_transformers") is None:
            raise VectorProviderError(
                "dependency_missing",
                "vector dependencies are not installed; install the vector extra before enabling local retrieval",
            )

        cache_key = (str(self.model_path), self.device, self.max_sequence_length)
        with _LOCAL_MODEL_CACHE_LOCK:
            cached = _LOCAL_MODEL_CACHE.get(cache_key)
        if cached is not None:
            self._model, self._identity = cached
            return self._model

        # These flags make accidental hub access fail closed even if a future
        # library version changes its default behaviour.  The constructor's
        # local_files_only argument remains the primary per-load guard.
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
        os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
        try:
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(
                str(self.model_path),
                device=self.device,
                local_files_only=True,
                trust_remote_code=False,
            )
            if not 64 <= self.max_sequence_length <= 8192:
                raise VectorProviderError("model_incompatible", "max_sequence_length must be between 64 and 8192")
            model.max_seq_length = min(self.max_sequence_length, model.max_seq_length or self.max_sequence_length)
            dimension_getter = getattr(model, "get_embedding_dimension", model.get_sentence_embedding_dimension)
            dimensions = dimension_getter()
        except VectorProviderError:
            raise
        except Exception as exc:  # pragma: no cover - depends on optional runtime packages
            raise VectorProviderError("model_load_failed", "the local embedding model could not be loaded offline") from exc
        if not isinstance(dimensions, int) or dimensions != 1024:
            raise VectorProviderError("model_incompatible", "the local BGE-M3 model must report 1024 embedding dimensions")

        self._model = model
        self._identity = VectorProviderIdentity(
            provider_id=self.provider_id,
            model_id=self.model_path.name,
            model_version=f"{_local_model_version(self.model_path)}+seq{model.max_seq_length}",
            dimensions=dimensions,
        )
        with _LOCAL_MODEL_CACHE_LOCK:
            cached = _LOCAL_MODEL_CACHE.setdefault(cache_key, (self._model, self._identity))
        self._model, self._identity = cached
        return self._model


def local_provider_readiness(model_path: str | Path | None) -> dict[str, object]:
    """Inspect configuration without importing optional packages or loading a model."""

    if model_path is None:
        return {"available": False, "code": "model_missing"}
    path = Path(model_path).expanduser()
    if not path.is_dir():
        return {"available": False, "code": "model_missing"}
    if importlib.util.find_spec("sentence_transformers") is None:
        return {"available": False, "code": "dependency_missing"}
    return {"available": True, "code": "ready"}


def _local_model_version(model_path: Path) -> str:
    config = model_path / "config.json"
    try:
        raw = config.read_bytes()
        parsed = json.loads(raw.decode("utf-8"))
        revision = parsed.get("_commit_hash") if isinstance(parsed, dict) else None
        if isinstance(revision, str) and revision:
            return revision
        return hashlib.sha256(raw).hexdigest()[:16]
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return "local-unknown"


def _normalize(values: Sequence[float]) -> list[float]:
    magnitude = math.sqrt(sum(value * value for value in values))
    if magnitude == 0:
        raise VectorProviderError("invalid_embedding", "embedding provider returned a zero vector")
    return [value / magnitude for value in values]
