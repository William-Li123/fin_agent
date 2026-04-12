from __future__ import annotations


class MemoryConfigError(ValueError):
    """Raised when memory configuration cannot be resolved."""


class MemoryStoreError(RuntimeError):
    """Raised when the Oracle-backed memory store cannot complete an operation."""


class MemoryModelNotReadyError(RuntimeError):
    """Raised when the local embedding model is missing or unusable."""

