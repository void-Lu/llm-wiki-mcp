"""Persistent, explicit lifecycle management for local vector indexes."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Iterable, Sequence

from runtime.runtime_provenance import RUNTIME_PROVENANCE
from runtime.runtime_config import EmbeddingSettings
from retrieval.vector_provider import VectorProvider, VectorProviderIdentity

VECTOR_INDEX_SCHEMA_VERSION = 2
DEFAULT_VECTOR_CANDIDATE_LIMIT = 50
DEFAULT_RRF_K = 60
DEFAULT_MIN_VECTOR_SCORE = 0.5

_DOCUMENT_READ_CACHE: dict[str, tuple[tuple[int, int], list[dict[str, object]]]] = {}
_DOCUMENT_READ_CACHE_LOCK = threading.Lock()


class VectorIndexError(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class VectorRecord:
    path: str
    content_hash: str
    text: str
    source_kind: str
    passage_id: str = ""
    page_path: str = ""
    corpus: str = "active"


@dataclass(frozen=True)
class VectorSearchResult:
    path: str
    score: float
    rank: int
    passage_id: str = ""


@dataclass(frozen=True)
class VectorSettings:
    provider: str
    model_path: Path | None
    index_path: Path
    candidate_limit: int
    rrf_k: int
    min_vector_score: float
    device: str
    batch_size: int
    max_sequence_length: int


def default_vector_index_path(vault_root: str | Path, *, corpus: str = "active") -> Path:
    name = "vector-index" if corpus == "active" else "archive-vector-index"
    return Path(vault_root).expanduser().resolve() / ".llm-wiki" / name


def parse_vector_settings(vault_root: str | Path, config: dict[str, Any] | None) -> VectorSettings:
    root = Path(vault_root).expanduser().resolve()
    values = config or {}
    if not isinstance(values, dict):
        raise VectorIndexError("vector_config_invalid", "vector_config must be an object")
    credential_keys = {"api_key", "token", "password", "secret", "authorization", "access_key"}
    forbidden = sorted(key for key in values if key.casefold() in credential_keys or key.casefold().endswith(("_token", "_secret", "_password")))
    if forbidden:
        raise VectorIndexError("vector_config_invalid", "provider credentials are not accepted through vector_config")
    provider = str(values.get("provider") or "local_bge_m3")
    if provider != "local_bge_m3":
        raise VectorIndexError("vector_provider_unsupported", "only the local_bge_m3 provider is supported")
    model_value = values.get("model_path")
    model_path = Path(model_value).expanduser().resolve() if isinstance(model_value, str) and model_value else None
    index_value = values.get("index_path")
    index_path = _resolved_index_path(root, index_value)
    return VectorSettings(
        provider=provider,
        model_path=model_path,
        index_path=index_path,
        candidate_limit=_bounded_int(values.get("candidate_limit"), DEFAULT_VECTOR_CANDIDATE_LIMIT, 1, 500, "candidate_limit"),
        rrf_k=_bounded_int(values.get("rrf_k"), DEFAULT_RRF_K, 1, 10_000, "rrf_k"),
        min_vector_score=_bounded_float(values.get("min_vector_score"), DEFAULT_MIN_VECTOR_SCORE, -1.0, 1.0, "min_vector_score"),
        device=str(values.get("device") or "cpu"),
        batch_size=_bounded_int(values.get("batch_size"), 16, 1, 256, "batch_size"),
        max_sequence_length=_bounded_int(values.get("max_sequence_length"), 256, 64, 8192, "max_sequence_length"),
    )


def vector_settings_from_embedding(vault_root: str | Path, embedding: EmbeddingSettings) -> VectorSettings:
    """Project a validated runtime snapshot into query/index settings.

    This is intentionally separate from ``parse_vector_settings``: the latter
    is only the compatibility decoder for the deprecated per-call object.
    """
    root = Path(vault_root).expanduser().resolve()
    return VectorSettings(
        provider=embedding.provider,
        model_path=embedding.model_path,
        index_path=embedding.index_path or default_vector_index_path(root),
        candidate_limit=embedding.candidate_limit,
        rrf_k=embedding.rrf_k,
        min_vector_score=embedding.min_vector_score,
        device=embedding.device,
        batch_size=embedding.batch_size,
        max_sequence_length=embedding.max_sequence_length,
    )


def _resolved_index_path(root: Path, value: object) -> Path:
    if value in (None, ""):
        return default_vector_index_path(root)
    if not isinstance(value, str):
        raise VectorIndexError("vector_config_invalid", "index_path must be a string")
    proposed = Path(value).expanduser()
    path = (root / proposed).resolve() if not proposed.is_absolute() else proposed.resolve()
    if not path.is_relative_to(root):
        raise VectorIndexError("vector_config_invalid", "index_path must remain inside the vault")
    return path


def _bounded_int(value: object, default: int, minimum: int, maximum: int, name: str) -> int:
    if value is None:
        return default
    if isinstance(value, bool):
        raise VectorIndexError("vector_config_invalid", f"{name} must be an integer")
    try:
        parsed = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise VectorIndexError("vector_config_invalid", f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise VectorIndexError("vector_config_invalid", f"{name} must be between {minimum} and {maximum}")
    return parsed


def _bounded_float(value: object, default: float, minimum: float, maximum: float, name: str) -> float:
    if value is None:
        return default
    if isinstance(value, bool):
        raise VectorIndexError("vector_config_invalid", f"{name} must be a number")
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        raise VectorIndexError("vector_config_invalid", f"{name} must be a number") from exc
    if not minimum <= parsed <= maximum:
        raise VectorIndexError("vector_config_invalid", f"{name} must be between {minimum} and {maximum}")
    return parsed


class VectorIndexStore:
    """A JSONL vector store with atomic full-build and incremental-update paths."""

    def __init__(self, vault_root: str | Path, index_path: str | Path | None = None, *, corpus: str = "active") -> None:
        self.root = Path(vault_root).expanduser().resolve()
        if corpus not in {"active", "archive"}:
            raise VectorIndexError("vector_config_invalid", "corpus must be active or archive")
        self.corpus = corpus
        self.index_path = Path(index_path).expanduser().resolve() if index_path is not None else default_vector_index_path(self.root, corpus=corpus)
        if not self.index_path.is_relative_to(self.root):
            raise VectorIndexError("vector_config_invalid", "index_path must remain inside the vault")

    @property
    def manifest_path(self) -> Path:
        return self.index_path / "manifest.json"

    @property
    def documents_path(self) -> Path:
        return self.index_path / "documents.jsonl"

    def status(
        self,
        records: Sequence[VectorRecord] | None = None,
        *,
        include_raw_sources: bool | None = None,
    ) -> dict[str, object]:
        try:
            manifest = self._read_manifest()
        except VectorIndexError as exc:
            return _status_error(exc.code, str(exc))
        expected_hashes = _record_hashes(records) if records is not None else None
        stale = _stale_details(manifest, expected_hashes, include_raw_sources)
        state = "stale" if stale["stale"] else "fresh"
        return {
            "ok": True,
            "state": state,
            "code": "index_stale" if stale["stale"] else "ready",
            "schema_version": manifest["schema_version"],
            "provider": {
                "provider_id": manifest["provider_id"],
                "model_id": manifest["model_id"],
                "model_version": manifest["model_version"],
            },
            "dimensions": manifest["dimensions"],
            "document_count": manifest["document_count"],
            "include_raw_sources": manifest["include_raw_sources"],
            "corpus": manifest["corpus"],
            "created_at": manifest["created_at"],
            "updated_at": manifest["updated_at"],
            "vault_fingerprint": manifest["vault_fingerprint"],
            "build_provenance": manifest["build_provenance"],
            "stale": stale,
        }

    def build(
        self,
        records: Sequence[VectorRecord],
        provider: VectorProvider,
        *,
        include_raw_sources: bool,
    ) -> dict[str, object]:
        started_at = perf_counter()
        stage_started_at = perf_counter()
        identity = provider.identity()
        model_load_ms = _elapsed_ms(stage_started_at)
        stage_started_at = perf_counter()
        vectors = provider.embed_documents([record.text for record in records])
        embed_documents_ms = _elapsed_ms(stage_started_at)
        stage_started_at = perf_counter()
        self._write_index(records, vectors, identity, include_raw_sources=include_raw_sources, created_at=_utc_now())
        write_index_ms = _elapsed_ms(stage_started_at)
        return {
            **self.status(records, include_raw_sources=include_raw_sources),
            "operation": "build",
            "timings_ms": {
                "model_load": model_load_ms,
                "embed_documents": embed_documents_ms,
                "write_index": write_index_ms,
                "total": _elapsed_ms(started_at),
            },
        }

    def update(
        self,
        records: Sequence[VectorRecord],
        provider: VectorProvider,
        *,
        include_raw_sources: bool,
    ) -> dict[str, object]:
        started_at = perf_counter()
        stage_started_at = perf_counter()
        manifest = self._read_manifest()
        read_manifest_ms = _elapsed_ms(stage_started_at)
        stage_started_at = perf_counter()
        identity = provider.identity()
        model_load_ms = _elapsed_ms(stage_started_at)
        stage_started_at = perf_counter()
        _assert_compatible(manifest, identity, include_raw_sources, self.corpus)
        existing = self._read_documents()
        read_documents_ms = _elapsed_ms(stage_started_at)
        stage_started_at = perf_counter()
        current_by_path = {_record_identity(record): record for record in records}
        previous_by_path = {_document_identity(item): item for item in existing}
        added = sorted(set(current_by_path) - set(previous_by_path))
        deleted = sorted(set(previous_by_path) - set(current_by_path))
        modified = sorted(
            path
            for path in set(current_by_path) & set(previous_by_path)
            if current_by_path[path].content_hash != previous_by_path[path].get("content_hash")
        )
        changed = added + modified
        compare_records_ms = _elapsed_ms(stage_started_at)
        stage_started_at = perf_counter()
        new_vectors = provider.embed_documents([current_by_path[path].text for path in changed])
        embed_documents_ms = _elapsed_ms(stage_started_at)
        stage_started_at = perf_counter()
        vectors_by_path = {path: vector for path, vector in zip(changed, new_vectors, strict=True)}
        for path, document in previous_by_path.items():
            if path in current_by_path and path not in vectors_by_path:
                vectors_by_path[path] = _vector_from_document(document)
        ordered_records = [current_by_path[path] for path in sorted(current_by_path)]
        ordered_vectors = [vectors_by_path[_record_identity(record)] for record in ordered_records]
        self._write_index(
            ordered_records,
            ordered_vectors,
            identity,
            include_raw_sources=include_raw_sources,
            created_at=str(manifest["created_at"]),
        )
        write_index_ms = _elapsed_ms(stage_started_at)
        return {
            **self.status(ordered_records, include_raw_sources=include_raw_sources),
            "operation": "update",
            "changes": {"added": len(added), "modified": len(modified), "deleted": len(deleted), "unchanged": len(records) - len(added) - len(modified)},
            "timings_ms": {
                "read_manifest": read_manifest_ms,
                "model_load": model_load_ms,
                "read_documents": read_documents_ms,
                "compare_records": compare_records_ms,
                "embed_documents": embed_documents_ms,
                "write_index": write_index_ms,
                "total": _elapsed_ms(started_at),
            },
        }

    def search(
        self,
        query_vector: Sequence[float],
        *,
        allowed_paths: set[str] | None = None,
        allowed_ids: set[str] | None = None,
        limit: int,
    ) -> list[VectorSearchResult]:
        manifest = self._read_manifest()
        dimensions = int(manifest["dimensions"])  # type: ignore[arg-type]
        if len(query_vector) != dimensions:
            raise VectorIndexError("index_incompatible", "query embedding dimensions do not match the index")
        scored = []
        for document in self._read_documents():
            path = str(document.get("page_path") or document["path"])
            passage_id = _document_identity(document)
            if allowed_ids is not None and passage_id not in allowed_ids:
                continue
            if allowed_paths is not None and path not in allowed_paths:
                continue
            vector = _vector_from_document(document)
            score = round(sum(left * right for left, right in zip(query_vector, vector, strict=True)), 12)
            scored.append((path, passage_id, score))
        return [
            VectorSearchResult(path=path, passage_id=passage_id, score=score, rank=rank)
            for rank, (path, passage_id, score) in enumerate(
                sorted(scored, key=lambda item: (-item[2], item[0], item[1]))[:limit], 1
            )
        ]

    def validate_provider(self, identity: VectorProviderIdentity, *, include_raw_sources: bool) -> None:
        """Reject a query when the loaded local model differs from its index."""

        _assert_compatible(self._read_manifest(), identity, include_raw_sources, self.corpus)

    def _read_manifest(self) -> dict[str, object]:
        if not self.manifest_path.exists() or not self.documents_path.exists():
            raise VectorIndexError("index_missing", "the vector index has not been built")
        try:
            manifest = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VectorIndexError("index_incompatible", "the vector index manifest is unreadable") from exc
        if not isinstance(manifest, dict) or manifest.get("schema_version") != VECTOR_INDEX_SCHEMA_VERSION:
            raise VectorIndexError("index_incompatible", "the vector index schema is incompatible")
        required = {
            "provider_id",
            "model_id",
            "model_version",
            "dimensions",
            "include_raw_sources",
            "created_at",
            "updated_at",
            "document_count",
            "vault_fingerprint",
            "build_provenance",
            "document_hashes",
            "corpus",
        }
        if not required <= set(manifest) or not isinstance(manifest["document_hashes"], dict):
            raise VectorIndexError("index_incompatible", "the vector index manifest is incomplete")
        return manifest

    def _read_documents(self) -> list[dict[str, object]]:
        try:
            stat = self.documents_path.stat()
        except OSError as exc:
            raise VectorIndexError("index_incompatible", "the vector index documents are unreadable") from exc
        signature = (stat.st_mtime_ns, stat.st_size)
        cache_key = str(self.documents_path)
        with _DOCUMENT_READ_CACHE_LOCK:
            cached = _DOCUMENT_READ_CACHE.get(cache_key)
        if cached is not None and cached[0] == signature:
            return cached[1]
        try:
            rows = [json.loads(line) for line in self.documents_path.read_text(encoding="utf-8").splitlines() if line]
        except (OSError, json.JSONDecodeError) as exc:
            raise VectorIndexError("index_incompatible", "the vector index documents are unreadable") from exc
        if not all(isinstance(row, dict) and {"path", "content_hash", "source_kind", "vector", "passage_id", "page_path", "corpus"} <= set(row) for row in rows):
            raise VectorIndexError("index_incompatible", "the vector index documents are invalid")
        with _DOCUMENT_READ_CACHE_LOCK:
            _DOCUMENT_READ_CACHE[cache_key] = (signature, rows)
        return rows

    def _write_index(
        self,
        records: Sequence[VectorRecord],
        vectors: Sequence[Sequence[float]],
        identity: VectorProviderIdentity,
        *,
        include_raw_sources: bool,
        created_at: str,
    ) -> None:
        if len(records) != len(vectors):
            raise VectorIndexError("invalid_embedding", "embedding count did not match document count")
        _validate_records(records)
        if any(record.corpus != self.corpus for record in records):
            raise VectorIndexError("index_incompatible", "vector records cannot cross the store corpus boundary")
        if any(len(vector) != identity.dimensions for vector in vectors):
            raise VectorIndexError("invalid_embedding", "embedding dimensions did not match the provider identity")
        self.index_path.parent.mkdir(parents=True, exist_ok=True)
        temp_path = Path(tempfile.mkdtemp(prefix="vector-index-", dir=self.index_path.parent))
        try:
            documents = [
                {
                    "path": record.path,
                    "passage_id": _record_identity(record),
                    "page_path": record.page_path or record.path,
                    "corpus": record.corpus,
                    "content_hash": record.content_hash,
                    "source_kind": record.source_kind,
                    "vector": [round(float(value), 12) for value in vector],
                }
                for record, vector in sorted(zip(records, vectors, strict=True), key=lambda item: _record_identity(item[0]))
            ]
            manifest = {
                "schema_version": VECTOR_INDEX_SCHEMA_VERSION,
                **identity.to_public_dict(),
                "include_raw_sources": include_raw_sources,
                "created_at": created_at,
                "updated_at": _utc_now(),
                "document_count": len(documents),
                "vault_fingerprint": _vault_fingerprint(records),
                "build_provenance": RUNTIME_PROVENANCE.to_public_dict(),
                "document_hashes": _record_hashes(records),
                "corpus": self.corpus,
            }
            (temp_path / "documents.jsonl").write_text(
                "".join(json.dumps(document, ensure_ascii=False, sort_keys=True) + "\n" for document in documents),
                encoding="utf-8",
            )
            (temp_path / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            self._replace_atomically(temp_path)
            with _DOCUMENT_READ_CACHE_LOCK:
                _DOCUMENT_READ_CACHE.pop(str(self.documents_path), None)
        except Exception:
            if temp_path.exists():
                shutil.rmtree(temp_path, ignore_errors=True)
            raise

    def _replace_atomically(self, staged: Path) -> None:
        backup = self.index_path.with_name(f"{self.index_path.name}.previous")
        if backup.exists():
            shutil.rmtree(backup)
        if not self.index_path.exists():
            os.replace(staged, self.index_path)
            return
        os.replace(self.index_path, backup)
        try:
            os.replace(staged, self.index_path)
        except Exception:
            os.replace(backup, self.index_path)
            raise
        shutil.rmtree(backup)


def _status_error(code: str, message: str) -> dict[str, object]:
    return {"ok": False, "state": "missing" if code == "index_missing" else "incompatible", "code": code, "error": message}


def _record_hashes(records: Sequence[VectorRecord] | None) -> dict[str, str]:
    return {_record_identity(record): record.content_hash for record in sorted(records or (), key=_record_identity)}


def _record_identity(record: VectorRecord) -> str:
    return record.passage_id or record.path


def _document_identity(document: dict[str, object]) -> str:
    return str(document.get("passage_id") or document["path"])


def _stale_details(manifest: dict[str, object], expected_hashes: dict[str, str] | None, include_raw_sources: bool | None) -> dict[str, object]:
    current = manifest["document_hashes"]
    assert isinstance(current, dict)
    current_hashes = {str(path): str(content_hash) for path, content_hash in current.items()}
    if expected_hashes is None:
        return {"stale": False, "added": 0, "modified": 0, "deleted": 0, "include_raw_sources_changed": False}
    added = set(expected_hashes) - set(current_hashes)
    deleted = set(current_hashes) - set(expected_hashes)
    modified = {path for path in set(expected_hashes) & set(current_hashes) if expected_hashes[path] != current_hashes[path]}
    raw_changed = include_raw_sources is not None and bool(manifest["include_raw_sources"]) != include_raw_sources
    return {
        "stale": bool(added or modified or deleted or raw_changed),
        "added": len(added),
        "modified": len(modified),
        "deleted": len(deleted),
        "include_raw_sources_changed": raw_changed,
    }


def _assert_compatible(manifest: dict[str, object], identity: VectorProviderIdentity, include_raw_sources: bool, corpus: str = "active") -> None:
    expected = identity.to_public_dict()
    for key, value in expected.items():
        if manifest.get(key) != value:
            raise VectorIndexError("index_incompatible", "provider or model changed; run a full vector build")
    if bool(manifest.get("include_raw_sources")) != include_raw_sources:
        raise VectorIndexError("index_incompatible", "raw-source policy changed; run a full vector build")
    if manifest.get("corpus") != corpus:
        raise VectorIndexError("index_incompatible", "corpus boundary changed; run a full vector build")


def _vector_from_document(document: dict[str, object]) -> list[float]:
    values = document.get("vector")
    if not isinstance(values, list) or not all(isinstance(value, (int, float)) for value in values):
        raise VectorIndexError("index_incompatible", "the vector index contains an invalid embedding")
    return [float(value) for value in values]


def _validate_records(records: Iterable[VectorRecord]) -> None:
    identities: set[str] = set()
    for record in records:
        identity = _record_identity(record)
        page_path = record.page_path or record.path
        if not identity or not page_path or identity in identities or Path(page_path).is_absolute() or ".." in Path(page_path).parts:
            raise VectorIndexError("index_incompatible", "vector passage IDs must be unique and page paths vault-relative")
        if record.corpus not in {"active", "archive"}:
            raise VectorIndexError("index_incompatible", "vector records must declare an allowed corpus")
        if not record.content_hash:
            raise VectorIndexError("index_incompatible", "vector documents must include a content hash")
        identities.add(identity)


def _vault_fingerprint(records: Sequence[VectorRecord]) -> str:
    material = "\n".join(f"{record.corpus}\0{_record_identity(record)}\0{record.content_hash}" for record in sorted(records, key=_record_identity))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _elapsed_ms(started_at: float) -> float:
    return round((perf_counter() - started_at) * 1000, 3)
