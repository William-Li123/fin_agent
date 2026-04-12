from __future__ import annotations

from typing import Sequence

from .schemas import DocumentChunk, SourceDocument
from .utils import stable_chunk_id


class OverlappingTextChunker:
    """A simple recursive-ish character chunker with explicit overlap."""

    def __init__(
        self,
        *,
        chunk_size: int,
        chunk_overlap: int,
        min_chunk_chars: int = 120,
        separators: Sequence[str] | None = None,
    ) -> None:
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.min_chunk_chars = min_chunk_chars
        self.separators = list(separators or ["\n\n", "\n", ". ", "。", "! ", "！", "? ", "？", " "])

    def chunk(self, document: SourceDocument) -> list[DocumentChunk]:
        text = document.text
        if not text:
            return []

        spans = self._split_spans(text)
        chunks: list[DocumentChunk] = []
        total = len(spans)
        for chunk_index, (start_char, end_char) in enumerate(spans):
            chunk_text = text[start_char:end_char]
            metadata = dict(document.metadata)
            chunks.append(
                DocumentChunk(
                    chunk_id=stable_chunk_id(document.source_id, chunk_index),
                    source_id=document.source_id,
                    source_path=str(document.path),
                    source_name=document.path.name,
                    chunk_index=chunk_index,
                    chunk_count=total,
                    text=chunk_text,
                    start_char=start_char,
                    end_char=end_char,
                    metadata=metadata,
                )
            )
        return chunks

    def _split_spans(self, text: str) -> list[tuple[int, int]]:
        spans: list[tuple[int, int]] = []
        text_length = len(text)
        start = 0

        while start < text_length:
            hard_end = min(start + self.chunk_size, text_length)
            end = hard_end

            if hard_end < text_length:
                boundary = self._find_boundary(text[start:hard_end])
                if boundary is not None:
                    end = start + boundary

            normalized_start, normalized_end = self._trim_span(text, start, end)
            if normalized_end > normalized_start:
                spans.append((normalized_start, normalized_end))

            if end >= text_length:
                break

            next_start = max(end - self.chunk_overlap, 0)
            if next_start <= start:
                next_start = end
            start = next_start

        return spans

    def _find_boundary(self, window: str) -> int | None:
        best_cut: int | None = None
        for separator in self.separators:
            index = window.rfind(separator)
            if index < 0:
                continue
            candidate = index + len(separator)
            if candidate < self.min_chunk_chars:
                continue
            if best_cut is None or candidate > best_cut:
                best_cut = candidate
        return best_cut

    def _trim_span(self, text: str, start: int, end: int) -> tuple[int, int]:
        while start < end and text[start].isspace():
            start += 1
        while end > start and text[end - 1].isspace():
            end -= 1
        return start, end
