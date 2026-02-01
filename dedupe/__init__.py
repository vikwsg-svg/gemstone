"""
Deduplication module using perceptual hashing.

This module provides:
- Image perceptual hashing (phash)
- URL canonicalization
- Duplicate detection and tracking
"""

from dedupe.phash import (
    compute_phash,
    compare_phashes,
    is_duplicate_image,
    ImageHasher,
)
from dedupe.url_canonical import (
    canonicalize_url,
    is_duplicate_url,
    URLCanonicalizer,
)
from dedupe.deduplicator import (
    Deduplicator,
    DuplicateResult,
)

__all__ = [
    "compute_phash",
    "compare_phashes",
    "is_duplicate_image",
    "ImageHasher",
    "canonicalize_url",
    "is_duplicate_url",
    "URLCanonicalizer",
    "Deduplicator",
    "DuplicateResult",
]
