from __future__ import annotations


class RAGError(Exception):
    """Base error for the local RAG subsystem."""


class RAGConfigError(RAGError):
    """Raised when the RAG subsystem cannot be configured."""


class DocumentConversionError(RAGError):
    """Raised when an input file cannot be converted into retrieval text."""


class ModelNotReadyError(RAGError):
    """Raised when a local embedding or reranking model is not ready to use."""


class VectorStoreError(RAGError):
    """Raised when the Qdrant vector store fails."""
