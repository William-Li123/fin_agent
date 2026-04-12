from __future__ import annotations

from pathlib import Path

from markitdown import MarkItDown

from .errors import DocumentConversionError
from .schemas import SourceDocument
from .utils import infer_mime_type, normalize_text, stable_source_id


class MarkItDownDocumentConverter:
    """Convert local files into retrieval text via MarkItDown."""

    def __init__(self) -> None:
        self._converter = MarkItDown()

    def convert(self, path: str | Path) -> SourceDocument:
        source_path = Path(path).resolve()
        if not source_path.exists():
            raise DocumentConversionError(f"File does not exist: {source_path}")
        if not source_path.is_file():
            raise DocumentConversionError(f"Expected a file path, got directory: {source_path}")

        try:
            result = self._converter.convert(source_path)
        except Exception as exc:
            raise DocumentConversionError(f"Failed to convert {source_path}: {exc}") from exc

        text = normalize_text(getattr(result, "text_content", "") or "")
        if not text:
            raise DocumentConversionError(f"Converted file is empty: {source_path}")

        stat = source_path.stat()
        title = getattr(result, "title", None)
        normalized_title = str(title).strip() or None if title is not None else None

        metadata = {
            "title": normalized_title,
            "mime_type": infer_mime_type(source_path),
            "file_name": source_path.name,
            "extension": source_path.suffix.lower(),
            "size_bytes": stat.st_size,
            "modified_timestamp": int(stat.st_mtime),
        }

        return SourceDocument(
            source_id=stable_source_id(source_path),
            path=source_path,
            title=normalized_title,
            text=text,
            metadata=metadata,
        )
