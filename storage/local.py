"""
Local filesystem storage backend.

Stores data in the local filesystem with organized directory structure.
"""

import shutil
from pathlib import Path
from typing import BinaryIO, Iterator, Optional, Union

import structlog

from config.settings import get_settings
from storage.base import StorageBackend

logger = structlog.get_logger(__name__)


class LocalStorage(StorageBackend):
    """
    Local filesystem storage backend.

    Stores files in an organized directory structure under the root path.
    """

    def __init__(self, root_path: Optional[str] = None):
        """
        Initialize local storage.

        Args:
            root_path: Root directory for storage
        """
        settings = get_settings()
        self.root = Path(root_path or settings.local_storage_root)
        self.root.mkdir(parents=True, exist_ok=True)
        logger.info("local_storage_initialized", root=str(self.root))

    def _resolve_path(self, key: str) -> Path:
        """Resolve a key to a full path."""
        # Normalize key
        key = key.lstrip("/")
        return self.root / key

    def put(
        self,
        key: str,
        data: Union[bytes, BinaryIO, str],
        content_type: Optional[str] = None,
    ) -> str:
        """Store data at the given key."""
        path = self._resolve_path(key)
        path.parent.mkdir(parents=True, exist_ok=True)

        try:
            if isinstance(data, str):
                with open(path, "w", encoding="utf-8") as f:
                    f.write(data)
            elif isinstance(data, bytes):
                with open(path, "wb") as f:
                    f.write(data)
            else:
                # File-like object
                with open(path, "wb") as f:
                    shutil.copyfileobj(data, f)

            logger.debug("stored_file", key=key, path=str(path))
            return str(path)

        except Exception as e:
            logger.error("storage_put_error", key=key, error=str(e))
            raise

    def get(self, key: str) -> Optional[bytes]:
        """Retrieve data from the given key."""
        path = self._resolve_path(key)

        if not path.exists():
            return None

        try:
            with open(path, "rb") as f:
                return f.read()
        except Exception as e:
            logger.error("storage_get_error", key=key, error=str(e))
            return None

    def exists(self, key: str) -> bool:
        """Check if a key exists."""
        path = self._resolve_path(key)
        return path.exists()

    def delete(self, key: str) -> bool:
        """Delete data at the given key."""
        path = self._resolve_path(key)

        if not path.exists():
            return False

        try:
            if path.is_file():
                path.unlink()
            else:
                shutil.rmtree(path)
            logger.debug("deleted_file", key=key)
            return True
        except Exception as e:
            logger.error("storage_delete_error", key=key, error=str(e))
            return False

    def list_keys(self, prefix: str = "") -> Iterator[str]:
        """List all keys with the given prefix."""
        base_path = self._resolve_path(prefix)

        if base_path.is_file():
            yield prefix
            return

        if not base_path.exists():
            return

        for path in base_path.rglob("*"):
            if path.is_file():
                # Return key relative to root
                yield str(path.relative_to(self.root))

    def get_url(self, key: str) -> str:
        """Get a path for accessing the stored data."""
        path = self._resolve_path(key)
        return str(path.absolute())

    def get_size(self, key: str) -> Optional[int]:
        """Get the size of stored data."""
        path = self._resolve_path(key)
        if path.exists():
            return path.stat().st_size
        return None

    def copy(self, src_key: str, dst_key: str) -> bool:
        """Copy data from one key to another."""
        src_path = self._resolve_path(src_key)
        dst_path = self._resolve_path(dst_key)

        if not src_path.exists():
            return False

        try:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_path, dst_path)
            return True
        except Exception as e:
            logger.error("storage_copy_error", src=src_key, dst=dst_key, error=str(e))
            return False

    def move(self, src_key: str, dst_key: str) -> bool:
        """Move data from one key to another."""
        src_path = self._resolve_path(src_key)
        dst_path = self._resolve_path(dst_key)

        if not src_path.exists():
            return False

        try:
            dst_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src_path, dst_path)
            return True
        except Exception as e:
            logger.error("storage_move_error", src=src_key, dst=dst_key, error=str(e))
            return False
