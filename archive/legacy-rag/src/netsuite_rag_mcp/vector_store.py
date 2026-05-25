from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

import chromadb
import chromadb.errors
from sentence_transformers import SentenceTransformer

from netsuite_rag_mcp.metadata import from_chroma_metadata, to_chroma_metadata
from netsuite_rag_mcp.models import Chunk, SearchResult


@runtime_checkable
class Embedder(Protocol):
    """Protocol for embedding providers."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed a list of texts and return list of embedding vectors."""
        ...


class SentenceTransformerEmbedder:
    """Wrapper around SentenceTransformer model."""

    def __init__(self, model_name: str, cache_folder: str | Path | None = None):
        kwargs = {"cache_folder": str(cache_folder)} if cache_folder is not None else {}
        self.model = SentenceTransformer(model_name, **kwargs)

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Embed texts using SentenceTransformer and return normalized embeddings as lists."""
        embeddings = self.model.encode(texts, normalize_embeddings=True)
        return [embedding.tolist() for embedding in embeddings]


class FakeEmbedder:
    """Deterministic embedder for testing that produces 4-float vectors."""

    def embed(self, texts: list[str]) -> list[list[float]]:
        """Generate deterministic embeddings based on text content."""
        embeddings = []
        for text in texts:
            # Count patterns in text (case-insensitive)
            text_lower = text.lower()

            # Count "order" or "订单"
            order_count = len(re.findall(r"order|订单", text_lower))

            # Count "restlet"
            restlet_count = len(re.findall(r"restlet", text_lower))

            # Count "map/reduce" or "mapreduce" or "map reduce"
            mapreduce_count = len(
                re.findall(r"map\s*/\s*reduce|mapreduce|map\s+reduce", text_lower)
            )

            # len(text) % 17
            len_mod = len(text) % 17

            # Create 4-float vector
            embedding = [
                float(order_count),
                float(restlet_count),
                float(mapreduce_count),
                float(len_mod),
            ]
            embeddings.append(embedding)

        return embeddings


class ChromaVectorStore:
    """Vector store wrapper using Chroma with configurable embeddings."""

    def __init__(self, persist_path: Path, collection_name: str, embedder: Embedder):
        """Initialize ChromaVectorStore with persistent storage and embedder.

        Args:
            persist_path: Path to store Chroma database
            collection_name: Name of the Chroma collection
            embedder: Embedder instance (SentenceTransformerEmbedder, FakeEmbedder, etc.)
        """
        self.persist_path = Path(persist_path)
        self.collection_name = collection_name
        self.embedder = embedder

        # Create persistent Chroma client
        self.client = chromadb.PersistentClient(path=str(self.persist_path))

        # Get or create collection
        self._collection = None
        self._init_collection()

    def _init_collection(self) -> None:
        """Initialize or retrieve the collection."""
        try:
            self._collection = self.client.get_collection(name=self.collection_name)
        except chromadb.errors.NotFoundError:
            # Collection doesn't exist, create it
            self._collection = self.client.create_collection(
                name=self.collection_name, metadata={"hnsw:space": "cosine"}
            )

    def reset(self) -> None:
        """Delete and recreate the collection."""
        try:
            self.client.delete_collection(name=self.collection_name)
        except chromadb.errors.NotFoundError:
            # Collection doesn't exist, ignore
            pass
        self._init_collection()

    def count(self) -> int:
        """Return the number of documents in the collection."""
        return self._collection.count()

    def delete_doc(self, doc_id: str) -> None:
        """Delete all chunks belonging to a document by doc_id."""
        # Get all documents with this doc_id and delete them
        results = self._collection.get(
            where={"doc_id": doc_id}, include=[]
        )
        if results["ids"]:
            self._collection.delete(ids=results["ids"])

    @staticmethod
    def _metadata_for_chunk(chunk: Chunk) -> dict[str, Any]:
        """Return Chroma metadata with Chunk top-level fields included."""
        metadata = dict(chunk.metadata)
        metadata["doc_id"] = chunk.doc_id
        metadata["chunk_index"] = chunk.chunk_index
        metadata["source_path"] = chunk.source_path
        metadata["heading"] = chunk.heading

        if chunk.function_name or "function_name" not in metadata:
            metadata["function_name"] = chunk.function_name
        if chunk.line_start or "line_start" not in metadata:
            metadata["line_start"] = chunk.line_start
        if chunk.line_end or "line_end" not in metadata:
            metadata["line_end"] = chunk.line_end
        if chunk.source_kind != "note" or "source_kind" not in metadata:
            metadata["source_kind"] = chunk.source_kind
        if chunk.source_name or "source_name" not in metadata:
            metadata["source_name"] = chunk.source_name
        if chunk.file_hash or "file_hash" not in metadata:
            metadata["file_hash"] = chunk.file_hash

        return metadata

    def upsert_chunks(self, chunks: list[Chunk]) -> None:
        """Upsert chunks into the vector store.

        Args:
            chunks: List of Chunk objects to store
        """
        if not chunks:
            return

        # Extract components for Chroma
        ids = [chunk.id for chunk in chunks]
        texts = [chunk.text for chunk in chunks]
        embeddings = self.embedder.embed(texts)

        # Convert metadata for Chroma storage
        chroma_metadatas = [to_chroma_metadata(self._metadata_for_chunk(chunk)) for chunk in chunks]

        # Upsert into collection
        self._collection.upsert(
            ids=ids, documents=texts, embeddings=embeddings, metadatas=chroma_metadatas
        )

    def query(self, query_text: str, n_results: int = 5, where: dict[str, Any] | None = None) -> list[SearchResult]:
        """Query the vector store for similar chunks.

        Args:
            query_text: Text to search for
            n_results: Number of results to return
            where: Optional ChromaDB metadata filter where clause

        Returns:
            List of SearchResult objects
        """
        # Embed query
        query_embedding = self.embedder.embed([query_text])[0]

        # Build query kwargs
        query_kwargs: dict[str, Any] = {
            "query_embeddings": [query_embedding],
            "n_results": n_results,
            "include": ["documents", "metadatas", "distances"],
        }
        if where is not None:
            query_kwargs["where"] = where

        # Query Chroma collection
        results = self._collection.query(**query_kwargs)

        # Convert results to SearchResult objects
        search_results = []
        if results["ids"] and len(results["ids"]) > 0:
            for i, chunk_id in enumerate(results["ids"][0]):
                # Generate citation ID (S1, S2, ...)
                citation_id = f"S{i + 1}"

                # Get text and metadata
                text = results["documents"][0][i] if results["documents"] else ""
                chroma_metadata = results["metadatas"][0][i] if results["metadatas"] else {}
                distance = results["distances"][0][i] if results["distances"] else None

                # Convert metadata back from Chroma format
                metadata = from_chroma_metadata(chroma_metadata)

                search_result = SearchResult(
                    citation_id=citation_id,
                    chunk_id=chunk_id,
                    text=text,
                    metadata=metadata,
                    distance=distance,
                )
                search_results.append(search_result)

        return search_results