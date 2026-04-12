from __future__ import annotations

from collections import OrderedDict
from typing import Any, Sequence

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, FieldCondition, Filter, MatchAny, MatchValue, PointStruct, VectorParams

from .config import QdrantSettings
from .errors import VectorStoreError
from .schemas import DocumentChunk, IndexedSource, RetrievedChunk

DISTANCE_MAP = {
    "cosine": Distance.COSINE,
    "dot": Distance.DOT,
    "euclid": Distance.EUCLID,
}


class QdrantVectorStore:
    def __init__(self, settings: QdrantSettings) -> None:
        self.settings = settings
        self._client = self._build_client()
        self._vector_size: int | None = None

    def collection_name(self) -> str:
        return self.settings.collection_name

    def is_ready(self) -> bool:
        try:
            return self._client.collection_exists(self.settings.collection_name)
        except Exception:
            return False

    def describe_status(self) -> dict[str, Any]:
        details: dict[str, Any] = {
            "mode": self.settings.mode,
            "collection_name": self.settings.collection_name,
            "ready": self.is_ready(),
        }
        if self.settings.mode == "local":
            details["path"] = str(self.settings.path)
        else:
            details["url"] = self.settings.url
        return details

    def ensure_collection(self, vector_size: int) -> None:
        try:
            if self._client.collection_exists(self.settings.collection_name):
                collection_info = self._client.get_collection(self.settings.collection_name)
                existing_vectors = getattr(collection_info.config.params, "vectors", None)
                existing_size = getattr(existing_vectors, "size", None)
                if existing_size is not None and int(existing_size) != int(vector_size):
                    raise VectorStoreError(
                        f"Existing Qdrant collection '{self.settings.collection_name}' expects vector size "
                        f"{existing_size}, but the embedder produced {vector_size}."
                    )
            else:
                self._client.create_collection(
                    collection_name=self.settings.collection_name,
                    vectors_config=VectorParams(
                        size=vector_size,
                        distance=DISTANCE_MAP[self.settings.distance],
                    ),
                    on_disk_payload=self.settings.on_disk_payload,
                )
            self._vector_size = vector_size
        except VectorStoreError:
            raise
        except Exception as exc:
            raise VectorStoreError(f"Failed to ensure Qdrant collection: {exc}") from exc

    def upsert_chunks(self, chunks: Sequence[DocumentChunk], vectors: Sequence[Sequence[float]]) -> None:
        if len(chunks) != len(vectors):
            raise VectorStoreError("Chunk and vector counts must match before upserting.")

        points = [
            PointStruct(
                id=chunk.chunk_id,
                vector=list(vector),
                payload=self._build_payload(chunk),
            )
            for chunk, vector in zip(chunks, vectors, strict=True)
        ]
        try:
            self._client.upsert(collection_name=self.settings.collection_name, points=points)
        except Exception as exc:
            raise VectorStoreError(f"Failed to upsert points into Qdrant: {exc}") from exc

    def search(
        self,
        query_vector: Sequence[float],
        *,
        limit: int,
        score_threshold: float | None = None,
        source_ids: Sequence[str] | None = None,
        source_paths: Sequence[str] | None = None,
    ) -> list[RetrievedChunk]:
        try:
            response = self._client.query_points(
                collection_name=self.settings.collection_name,
                query=list(query_vector),
                limit=limit,
                with_payload=True,
                with_vectors=False,
                query_filter=self._build_filter(source_ids=source_ids, source_paths=source_paths),
                score_threshold=score_threshold,
            )
        except Exception as exc:
            raise VectorStoreError(f"Failed to query Qdrant: {exc}") from exc

        hits: list[RetrievedChunk] = []
        for point in response.points:
            payload = point.payload or {}
            hits.append(
                RetrievedChunk(
                    chunk_id=str(point.id),
                    source_id=str(payload.get("source_id", "")),
                    source_path=str(payload.get("source_path", "")),
                    source_name=str(payload.get("source_name", "")),
                    chunk_index=int(payload.get("chunk_index", 0)),
                    chunk_count=int(payload.get("chunk_count", 1)),
                    text=str(payload.get("text", "")),
                    score=float(point.score),
                    metadata=dict(payload.get("metadata", {})),
                )
            )
        return hits

    def delete_sources(
        self,
        *,
        source_ids: Sequence[str] | None = None,
        source_paths: Sequence[str] | None = None,
    ) -> None:
        selector = self._build_filter(source_ids=source_ids, source_paths=source_paths)
        if selector is None:
            return

        try:
            self._client.delete(collection_name=self.settings.collection_name, points_selector=selector)
        except Exception as exc:
            raise VectorStoreError(f"Failed to delete points from Qdrant: {exc}") from exc

    def list_indexed_sources(self, *, limit: int = 256) -> list[IndexedSource]:
        if not self.is_ready():
            return []

        deduplicated: OrderedDict[str, IndexedSource] = OrderedDict()
        offset = None
        page_limit = min(max(limit * 8, 64), 256)

        try:
            while True:
                records, offset = self._client.scroll(
                    collection_name=self.settings.collection_name,
                    limit=page_limit,
                    offset=offset,
                    with_payload=True,
                    with_vectors=False,
                )
                if not records:
                    break

                for record in records:
                    payload = record.payload or {}
                    source_id = str(payload.get("source_id", ""))
                    if not source_id:
                        continue
                    if source_id not in deduplicated:
                        deduplicated[source_id] = IndexedSource(
                            source_id=source_id,
                            source_path=str(payload.get("source_path", "")),
                            source_name=str(payload.get("source_name", "")),
                            chunk_count=0,
                            metadata=dict(payload.get("metadata", {})),
                        )
                    deduplicated[source_id].chunk_count += 1

                if offset is None or len(deduplicated) >= limit:
                    break
        except Exception as exc:
            raise VectorStoreError(f"Failed to list indexed sources from Qdrant: {exc}") from exc

        return list(deduplicated.values())[:limit]

    def close(self) -> None:
        client = getattr(self, "_client", None)
        if client is None:
            return
        try:
            client.close()
        finally:
            self._client = _ClosedQdrantClient()

    def _build_client(self) -> QdrantClient:
        if self.settings.mode == "local":
            if self.settings.path is None:
                raise VectorStoreError("Local Qdrant mode requires a path.")
            self.settings.path.mkdir(parents=True, exist_ok=True)
            return QdrantClient(path=str(self.settings.path))

        if not self.settings.url:
            raise VectorStoreError("Remote Qdrant mode requires a URL.")
        return QdrantClient(url=self.settings.url, api_key=self.settings.api_key)

    def _build_filter(
        self,
        *,
        source_ids: Sequence[str] | None = None,
        source_paths: Sequence[str] | None = None,
    ) -> Filter | None:
        conditions: list[FieldCondition] = []
        normalized_source_ids = [item for item in (source_ids or []) if item]
        normalized_source_paths = [item for item in (source_paths or []) if item]

        if normalized_source_ids:
            if len(normalized_source_ids) == 1:
                conditions.append(FieldCondition(key="source_id", match=MatchValue(value=normalized_source_ids[0])))
            else:
                conditions.append(FieldCondition(key="source_id", match=MatchAny(any=normalized_source_ids)))

        if normalized_source_paths:
            if len(normalized_source_paths) == 1:
                conditions.append(FieldCondition(key="source_path", match=MatchValue(value=normalized_source_paths[0])))
            else:
                conditions.append(FieldCondition(key="source_path", match=MatchAny(any=normalized_source_paths)))

        if not conditions:
            return None
        return Filter(must=conditions)

    def _build_payload(self, chunk: DocumentChunk) -> dict[str, Any]:
        return {
            "source_id": chunk.source_id,
            "source_path": chunk.source_path,
            "source_name": chunk.source_name,
            "chunk_index": chunk.chunk_index,
            "chunk_count": chunk.chunk_count,
            "start_char": chunk.start_char,
            "end_char": chunk.end_char,
            "text": chunk.text,
            "metadata": dict(chunk.metadata),
        }


class _ClosedQdrantClient:
    def close(self) -> None:
        return None
