"""
Base storage interface.

Defines the abstract interface that all storage backends must implement.
"""

from abc import ABC, abstractmethod
from pathlib import Path
from typing import BinaryIO, Iterator, Optional, Union


class StorageBackend(ABC):
    """
    Abstract base class for storage backends.

    All storage backends (local, S3, etc.) must implement this interface.
    """

    @abstractmethod
    def put(
        self,
        key: str,
        data: Union[bytes, BinaryIO, str],
        content_type: Optional[str] = None,
    ) -> str:
        """
        Store data at the given key.

        Args:
            key: Storage key/path
            data: Data to store (bytes, file-like object, or string)
            content_type: Optional MIME type

        Returns:
            Full path/URL to stored data
        """
        pass

    @abstractmethod
    def get(self, key: str) -> Optional[bytes]:
        """
        Retrieve data from the given key.

        Args:
            key: Storage key/path

        Returns:
            Data as bytes, or None if not found
        """
        pass

    @abstractmethod
    def exists(self, key: str) -> bool:
        """
        Check if a key exists in storage.

        Args:
            key: Storage key/path

        Returns:
            True if key exists
        """
        pass

    @abstractmethod
    def delete(self, key: str) -> bool:
        """
        Delete data at the given key.

        Args:
            key: Storage key/path

        Returns:
            True if deleted successfully
        """
        pass

    @abstractmethod
    def list_keys(self, prefix: str = "") -> Iterator[str]:
        """
        List all keys with the given prefix.

        Args:
            prefix: Key prefix to filter by

        Yields:
            Keys matching the prefix
        """
        pass

    @abstractmethod
    def get_url(self, key: str) -> str:
        """
        Get a URL/path for accessing the stored data.

        Args:
            key: Storage key/path

        Returns:
            URL or path to access the data
        """
        pass

    def put_file(self, key: str, file_path: Union[str, Path]) -> str:
        """
        Store a file at the given key.

        Args:
            key: Storage key/path
            file_path: Path to file to store

        Returns:
            Full path/URL to stored data
        """
        with open(file_path, "rb") as f:
            return self.put(key, f)

    def get_to_file(self, key: str, file_path: Union[str, Path]) -> bool:
        """
        Download data to a file.

        Args:
            key: Storage key/path
            file_path: Path to save file to

        Returns:
            True if successful
        """
        data = self.get(key)
        if data is None:
            return False

        Path(file_path).parent.mkdir(parents=True, exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(data)
        return True
