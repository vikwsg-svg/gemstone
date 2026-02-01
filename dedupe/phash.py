"""
Perceptual hashing for image deduplication.

Uses imagehash library to compute perceptual hashes that are
similar for visually similar images.
"""

from io import BytesIO
from pathlib import Path
from typing import Optional, Union

import imagehash
from PIL import Image
import structlog

from config.settings import get_settings

logger = structlog.get_logger(__name__)


def compute_phash(
    image_source: Union[str, bytes, Path, Image.Image],
    hash_size: int = 16,
) -> Optional[str]:
    """
    Compute perceptual hash of an image.

    Args:
        image_source: Image path, bytes, or PIL Image
        hash_size: Size of hash (larger = more precise)

    Returns:
        Hex string of perceptual hash, or None on error
    """
    try:
        # Load image
        if isinstance(image_source, Image.Image):
            img = image_source
        elif isinstance(image_source, bytes):
            img = Image.open(BytesIO(image_source))
        elif isinstance(image_source, (str, Path)):
            img = Image.open(image_source)
        else:
            logger.error("invalid_image_source", source_type=type(image_source))
            return None

        # Convert to RGB if necessary
        if img.mode != "RGB":
            img = img.convert("RGB")

        # Compute perceptual hash
        phash = imagehash.phash(img, hash_size=hash_size)
        return str(phash)

    except Exception as e:
        logger.error("phash_computation_error", error=str(e))
        return None


def compare_phashes(hash1: str, hash2: str) -> int:
    """
    Compare two perceptual hashes.

    Args:
        hash1: First hash (hex string)
        hash2: Second hash (hex string)

    Returns:
        Hamming distance between hashes (0 = identical)
    """
    try:
        h1 = imagehash.hex_to_hash(hash1)
        h2 = imagehash.hex_to_hash(hash2)
        return h1 - h2
    except Exception as e:
        logger.error("phash_comparison_error", error=str(e))
        return float("inf")


def is_duplicate_image(
    hash1: str,
    hash2: str,
    threshold: Optional[int] = None,
) -> bool:
    """
    Check if two images are duplicates based on perceptual hash.

    Args:
        hash1: First hash
        hash2: Second hash
        threshold: Maximum distance to consider duplicate

    Returns:
        True if images are likely duplicates
    """
    if threshold is None:
        settings = get_settings()
        threshold = settings.phash_distance_threshold

    distance = compare_phashes(hash1, hash2)
    return distance <= threshold


class ImageHasher:
    """
    Image hasher with caching and batch operations.

    Provides efficient hashing for large numbers of images.
    """

    def __init__(
        self,
        hash_size: int = 16,
        threshold: Optional[int] = None,
    ):
        """
        Initialize the image hasher.

        Args:
            hash_size: Size of hash (larger = more precise)
            threshold: Duplicate detection threshold
        """
        settings = get_settings()
        self.hash_size = hash_size
        self.threshold = threshold or settings.phash_distance_threshold
        self._cache: dict[str, str] = {}  # path -> hash

    def hash_image(
        self,
        image_source: Union[str, bytes, Path, Image.Image],
        cache_key: Optional[str] = None,
    ) -> Optional[str]:
        """
        Hash an image with optional caching.

        Args:
            image_source: Image to hash
            cache_key: Optional cache key (e.g., file path)

        Returns:
            Hash string or None
        """
        # Check cache
        if cache_key and cache_key in self._cache:
            return self._cache[cache_key]

        # Compute hash
        phash = compute_phash(image_source, self.hash_size)

        # Cache result
        if cache_key and phash:
            self._cache[cache_key] = phash

        return phash

    def is_duplicate(self, hash1: str, hash2: str) -> bool:
        """Check if two hashes indicate duplicates."""
        return is_duplicate_image(hash1, hash2, self.threshold)

    def find_duplicates(
        self,
        hashes: dict[str, str],
    ) -> list[tuple[str, str, int]]:
        """
        Find all duplicate pairs in a set of hashes.

        Args:
            hashes: Dictionary of id -> hash

        Returns:
            List of (id1, id2, distance) tuples for duplicates
        """
        duplicates = []
        ids = list(hashes.keys())

        for i, id1 in enumerate(ids):
            for id2 in ids[i + 1:]:
                distance = compare_phashes(hashes[id1], hashes[id2])
                if distance <= self.threshold:
                    duplicates.append((id1, id2, distance))

        return duplicates

    def find_nearest(
        self,
        query_hash: str,
        candidates: dict[str, str],
        max_distance: Optional[int] = None,
    ) -> list[tuple[str, int]]:
        """
        Find nearest matches to a query hash.

        Args:
            query_hash: Hash to search for
            candidates: Dictionary of id -> hash to search in
            max_distance: Maximum distance to return

        Returns:
            List of (id, distance) tuples, sorted by distance
        """
        if max_distance is None:
            max_distance = self.threshold

        matches = []
        for cand_id, cand_hash in candidates.items():
            distance = compare_phashes(query_hash, cand_hash)
            if distance <= max_distance:
                matches.append((cand_id, distance))

        return sorted(matches, key=lambda x: x[1])

    def clear_cache(self):
        """Clear the hash cache."""
        self._cache.clear()

    def get_image_dimensions(
        self,
        image_source: Union[str, bytes, Path],
    ) -> Optional[tuple[int, int]]:
        """
        Get image dimensions.

        Args:
            image_source: Image path or bytes

        Returns:
            Tuple of (width, height) or None
        """
        try:
            if isinstance(image_source, bytes):
                img = Image.open(BytesIO(image_source))
            else:
                img = Image.open(image_source)
            return img.size
        except Exception as e:
            logger.error("image_dimension_error", error=str(e))
            return None
