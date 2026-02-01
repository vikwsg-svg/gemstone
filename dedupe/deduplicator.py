"""
Main deduplicator combining URL and image hash deduplication.

Provides comprehensive duplicate detection for gemstone items.
"""

from dataclasses import dataclass, field
from typing import Optional

import structlog

from config.settings import get_settings
from dedupe.phash import ImageHasher, compute_phash
from dedupe.url_canonical import URLCanonicalizer, canonicalize_url
from scrapers.models import GemstoneItem

logger = structlog.get_logger(__name__)


@dataclass
class DuplicateResult:
    """Result of duplicate detection."""

    is_duplicate: bool
    duplicate_of: Optional[str] = None  # item_id of original
    duplicate_type: Optional[str] = None  # "url", "image", "both"
    confidence: float = 0.0
    details: dict = field(default_factory=dict)


class Deduplicator:
    """
    Comprehensive deduplicator for gemstone items.

    Combines:
    - URL canonicalization
    - Perceptual image hashing
    - Cross-site duplicate detection
    """

    def __init__(
        self,
        phash_threshold: Optional[int] = None,
    ):
        """
        Initialize the deduplicator.

        Args:
            phash_threshold: Threshold for image duplicate detection
        """
        settings = get_settings()
        self.phash_threshold = phash_threshold or settings.phash_distance_threshold

        self.url_canonicalizer = URLCanonicalizer()
        self.image_hasher = ImageHasher(threshold=self.phash_threshold)

        # Index of known items
        self._url_index: dict[str, str] = {}  # canonical_url -> item_id
        self._phash_index: dict[str, str] = {}  # phash -> item_id
        self._item_hashes: dict[str, list[str]] = {}  # item_id -> list of phashes

    def add_item(self, item: GemstoneItem) -> DuplicateResult:
        """
        Add an item to the index and check for duplicates.

        Args:
            item: The item to add

        Returns:
            DuplicateResult indicating if duplicate was found
        """
        # Check URL duplicate first
        canonical_url = canonicalize_url(item.source_url, item.source_site)
        item.canonical_url = canonical_url

        if canonical_url in self._url_index:
            original_id = self._url_index[canonical_url]
            logger.info(
                "url_duplicate_found",
                item_id=item.item_id,
                duplicate_of=original_id,
                url=canonical_url,
            )
            return DuplicateResult(
                is_duplicate=True,
                duplicate_of=original_id,
                duplicate_type="url",
                confidence=1.0,
                details={"canonical_url": canonical_url},
            )

        # Check image duplicates
        image_duplicate = self._check_image_duplicates(item)
        if image_duplicate.is_duplicate:
            return image_duplicate

        # Not a duplicate - add to index
        self._url_index[canonical_url] = item.item_id

        # Add image hashes to index
        item_hashes = []
        for img in item.downloaded_images:
            if img.phash:
                self._phash_index[img.phash] = item.item_id
                item_hashes.append(img.phash)
        self._item_hashes[item.item_id] = item_hashes

        return DuplicateResult(
            is_duplicate=False,
            confidence=0.0,
        )

    def _check_image_duplicates(self, item: GemstoneItem) -> DuplicateResult:
        """
        Check if any of the item's images match known images.

        Args:
            item: The item to check

        Returns:
            DuplicateResult
        """
        if not item.downloaded_images:
            return DuplicateResult(is_duplicate=False)

        # Check each image
        matching_items: dict[str, int] = {}  # item_id -> match count

        for img in item.downloaded_images:
            if not img.phash:
                continue

            # Find similar images
            matches = self.image_hasher.find_nearest(
                img.phash,
                self._phash_index,
                max_distance=self.phash_threshold,
            )

            for match_hash, distance in matches:
                original_id = self._phash_index.get(match_hash)
                if original_id and original_id != item.item_id:
                    matching_items[original_id] = matching_items.get(original_id, 0) + 1

        if not matching_items:
            return DuplicateResult(is_duplicate=False)

        # Find item with most matching images
        best_match = max(matching_items.items(), key=lambda x: x[1])
        original_id, match_count = best_match

        # Calculate confidence based on match ratio
        total_images = len([img for img in item.downloaded_images if img.phash])
        if total_images == 0:
            return DuplicateResult(is_duplicate=False)

        match_ratio = match_count / total_images

        # Consider duplicate if more than half of images match
        if match_ratio >= 0.5:
            logger.info(
                "image_duplicate_found",
                item_id=item.item_id,
                duplicate_of=original_id,
                match_count=match_count,
                total_images=total_images,
                ratio=match_ratio,
            )
            return DuplicateResult(
                is_duplicate=True,
                duplicate_of=original_id,
                duplicate_type="image",
                confidence=match_ratio,
                details={
                    "match_count": match_count,
                    "total_images": total_images,
                    "match_ratio": match_ratio,
                },
            )

        return DuplicateResult(
            is_duplicate=False,
            confidence=match_ratio,
            details={
                "partial_match": original_id,
                "match_count": match_count,
            },
        )

    def check_duplicate(self, item: GemstoneItem) -> DuplicateResult:
        """
        Check if an item is a duplicate without adding to index.

        Args:
            item: The item to check

        Returns:
            DuplicateResult
        """
        # Check URL
        canonical_url = canonicalize_url(item.source_url, item.source_site)

        if canonical_url in self._url_index:
            return DuplicateResult(
                is_duplicate=True,
                duplicate_of=self._url_index[canonical_url],
                duplicate_type="url",
                confidence=1.0,
            )

        # Check images
        return self._check_image_duplicates(item)

    def remove_item(self, item_id: str):
        """
        Remove an item from the index.

        Args:
            item_id: The item ID to remove
        """
        # Remove from URL index
        to_remove = [url for url, iid in self._url_index.items() if iid == item_id]
        for url in to_remove:
            del self._url_index[url]

        # Remove from phash index
        hashes = self._item_hashes.get(item_id, [])
        for h in hashes:
            if h in self._phash_index:
                del self._phash_index[h]

        # Remove from item hashes
        self._item_hashes.pop(item_id, None)

    def get_stats(self) -> dict:
        """Get deduplicator statistics."""
        return {
            "indexed_urls": len(self._url_index),
            "indexed_images": len(self._phash_index),
            "indexed_items": len(self._item_hashes),
        }

    def clear(self):
        """Clear all indices."""
        self._url_index.clear()
        self._phash_index.clear()
        self._item_hashes.clear()
        self.url_canonicalizer.clear_cache()
        self.image_hasher.clear_cache()


# Global instance
_deduplicator: Optional[Deduplicator] = None


def get_deduplicator() -> Deduplicator:
    """Get the global deduplicator instance."""
    global _deduplicator
    if _deduplicator is None:
        _deduplicator = Deduplicator()
    return _deduplicator
