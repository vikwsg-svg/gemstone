"""
Storage factory for creating storage backends.

Provides a unified interface for getting the configured storage backend.
"""

from typing import Optional

import structlog

from config.settings import get_settings
from storage.base import StorageBackend
from storage.local import LocalStorage
from storage.s3 import S3Storage

logger = structlog.get_logger(__name__)


_storage: Optional[StorageBackend] = None


def get_storage(backend_type: Optional[str] = None) -> StorageBackend:
    """
    Get the configured storage backend.

    Args:
        backend_type: Override the configured backend type ("local" or "s3")

    Returns:
        StorageBackend instance
    """
    global _storage

    settings = get_settings()
    backend = backend_type or settings.storage_backend

    if _storage is None or (backend_type and backend != settings.storage_backend):
        if backend == "s3":
            logger.info("creating_s3_storage")
            _storage = S3Storage()
        else:
            logger.info("creating_local_storage")
            _storage = LocalStorage()

    return _storage


def create_storage(backend_type: str, **kwargs) -> StorageBackend:
    """
    Create a new storage backend instance.

    Args:
        backend_type: Type of backend ("local" or "s3")
        **kwargs: Backend-specific arguments

    Returns:
        New StorageBackend instance
    """
    if backend_type == "s3":
        return S3Storage(**kwargs)
    else:
        return LocalStorage(**kwargs)
