"""
Storage layer supporting local filesystem and S3-compatible storage.

This module provides:
- Unified storage interface
- Local filesystem backend
- S3/MinIO backend
- Metadata database
"""

from storage.base import StorageBackend
from storage.local import LocalStorage
from storage.s3 import S3Storage
from storage.factory import get_storage
from storage.database import Database, get_database

__all__ = [
    "StorageBackend",
    "LocalStorage",
    "S3Storage",
    "get_storage",
    "Database",
    "get_database",
]
