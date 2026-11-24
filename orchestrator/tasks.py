"""
Celery tasks for the gemstone pipeline.

Tasks:
- discover_urls_task: Discover new URLs to crawl
- fetch_page_task: Fetch a page
- parse_page_task: Parse HTML and extract data
- validate_item_task: Validate and assign tier
- download_images_task: Download images for an item
- process_url_task: Complete pipeline for a single URL
"""

import hashlib
from datetime import datetime
from io import BytesIO
from pathlib import Path
from typing import Any, Optional
from uuid import uuid4

from celery import shared_task
from PIL import Image
import structlog

from config.allowed_sites import get_site_config, is_domain_allowed
from crawler.fetcher import Fetcher, FetchStatus
from crawler.robots import is_allowed_by_robots
from dedupe.deduplicator import get_deduplicator
from dedupe.phash import compute_phash
from scrapers.models import (
    ComplianceInfo,
    GemstoneItem,
    ScrapedImage,
    ValidationTier,
)
from scrapers.registry import get_scraper
from storage.database import get_database
from storage.factory import get_storage
from validators.tier import assign_tier

logger = structlog.get_logger(__name__)


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def discover_urls_task(
    self,
    seed_url: str,
    job_id: str,
    max_depth: int = 3,
    limit: int = 1000,
) -> dict:
    """
    Discover URLs from a seed URL.

    Args:
        seed_url: Starting URL
        job_id: Job ID for tracking
        max_depth: Maximum crawl depth
        limit: Maximum URLs to discover

    Returns:
        Dictionary with discovery results
    """
    from urllib.parse import urlparse

    logger.info("discover_urls_start", seed_url=seed_url, job_id=job_id)

    # Extract domain
    parsed = urlparse(seed_url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]

    # Check domain is allowed
    if not is_domain_allowed(domain):
        logger.error("domain_not_allowed", domain=domain)
        return {"error": f"Domain {domain} not in allowlist", "discovered": 0}

    # Get scraper
    scraper = get_scraper(domain)
    if not scraper:
        return {"error": f"No scraper for domain {domain}", "discovered": 0}

    # Initialize
    db = get_database()
    fetcher = Fetcher()

    discovered_count = 0
    to_visit = [(seed_url, 0)]
    visited = set()

    while to_visit and discovered_count < limit:
        url, depth = to_visit.pop(0)

        if url in visited or depth > max_depth:
            continue
        visited.add(url)

        # Check robots
        if not is_allowed_by_robots(url):
            logger.debug("robots_denied", url=url)
            continue

        # Fetch page
        result = fetcher.fetch(url, save_snapshot=False)
        if not result.success:
            continue

        # Add URL to database
        db.add_url({
            "url": url,
            "domain": domain,
            "status": "pending",
            "priority": 10 if scraper.is_listing_page(url, result.content) else 1,
            "depth": depth,
            "job_id": job_id,
        })
        discovered_count += 1

        # Discover more URLs
        if depth < max_depth:
            new_urls = scraper.discover_urls(result.content, url, depth)
            for new_url in new_urls:
                if new_url.url not in visited:
                    to_visit.append((new_url.url, new_url.depth))

    # Update job
    db.update_job(job_id, {"urls_discovered": discovered_count})

    logger.info(
        "discover_urls_complete",
        job_id=job_id,
        discovered=discovered_count,
    )

    return {
        "job_id": job_id,
        "domain": domain,
        "discovered": discovered_count,
    }


@shared_task(bind=True, max_retries=3, default_retry_delay=30)
def fetch_page_task(
    self,
    url: str,
    job_id: Optional[str] = None,
) -> dict:
    """
    Fetch a page and store the HTML.

    Args:
        url: URL to fetch
        job_id: Optional job ID

    Returns:
        Dictionary with fetch results
    """
    from urllib.parse import urlparse

    logger.info("fetch_page_start", url=url)

    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]

    # Check domain
    if not is_domain_allowed(domain):
        return {"error": "Domain not allowed", "url": url}

    # Check robots
    robots_allowed = is_allowed_by_robots(url)
    if not robots_allowed:
        return {"error": "Robots denied", "url": url, "robots_allowed": False}

    # Fetch
    fetcher = Fetcher()
    result = fetcher.fetch(url, save_snapshot=True)

    if not result.success:
        # Retry on temporary errors
        if result.status in (FetchStatus.TIMEOUT, FetchStatus.NETWORK_ERROR):
            raise self.retry(exc=Exception(result.error_message))
        return {
            "error": result.error_message,
            "url": url,
            "status": result.status.value,
        }

    # Update database
    db = get_database()
    db.update_url_status(url, "fetched")
    if job_id:
        job = db.get_job(job_id)
        if job:
            db.update_job(job_id, {"urls_fetched": job.get("urls_fetched", 0) + 1})

    logger.info("fetch_page_complete", url=url, content_length=result.content_length)

    return {
        "url": url,
        "content_length": result.content_length,
        "content": result.content,
        "robots_allowed": robots_allowed,
        "redirected_url": result.redirected_url,
    }


@shared_task(bind=True, max_retries=2)
def parse_page_task(
    self,
    url: str,
    html_content: str,
    robots_allowed: bool = True,
    job_id: Optional[str] = None,
) -> dict:
    """
    Parse HTML content and extract gemstone data.

    Args:
        url: Source URL
        html_content: HTML content to parse
        robots_allowed: Whether robots.txt allowed
        job_id: Optional job ID

    Returns:
        Dictionary with parsed item data
    """
    from urllib.parse import urlparse

    logger.info("parse_page_start", url=url)

    parsed = urlparse(url)
    domain = parsed.netloc.lower()
    if domain.startswith("www."):
        domain = domain[4:]

    # Get scraper
    scraper = get_scraper(domain)
    if not scraper:
        return {"error": f"No scraper for {domain}", "url": url}

    # Check if this is a listing page
    if not scraper.is_listing_page(url, html_content):
        logger.debug("not_listing_page", url=url)
        return {"skip": True, "reason": "Not a listing page", "url": url}

    # Create compliance info
    compliance = ComplianceInfo(
        robots_allowed=robots_allowed,
        tos_allowed=True,  # Pre-approved via allowlist
        user_agent="GemstoneBot/1.0",
    )

    # Extract item
    item = scraper.extract_item(html_content, url, compliance)

    if not item:
        return {"error": "Failed to extract item", "url": url}

    # Convert to dict for serialization
    item_dict = item.model_dump()

    logger.info(
        "parse_page_complete",
        url=url,
        stone_type=item.stone_type_claimed,
        images=len(item.image_urls),
    )

    return {
        "url": url,
        "item": item_dict,
        "job_id": job_id,
    }


@shared_task(bind=True)
def validate_item_task(
    self,
    item_dict: dict,
    job_id: Optional[str] = None,
) -> dict:
    """
    Validate an item and assign tier.

    Args:
        item_dict: Item data dictionary
        job_id: Optional job ID

    Returns:
        Dictionary with validation results
    """
    logger.info("validate_item_start", item_id=item_dict.get("item_id"))

    # Reconstruct item
    item = GemstoneItem(**item_dict)

    # Run validation
    validation_result = assign_tier(item)
    item.validation = validation_result

    # Check for duplicates
    deduplicator = get_deduplicator()
    dup_result = deduplicator.add_item(item)

    if dup_result.is_duplicate:
        item.is_duplicate = True
        item.duplicate_of = dup_result.duplicate_of
        logger.info(
            "duplicate_found",
            item_id=item.item_id,
            duplicate_of=dup_result.duplicate_of,
        )

    # Store in database
    db = get_database()
    db.add_item({
        "item_id": item.item_id,
        "source_site": item.source_site,
        "source_url": item.source_url,
        "canonical_url": item.canonical_url,
        "scraped_at": item.scraped_at_utc,
        "title": item.title,
        "description_text": item.description_text,
        "stone_type_claimed": item.stone_type_claimed,
        "shape": item.shape,
        "color_family": item.color_family,
        "carat": item.carat,
        "lab_name": item.lab_name,
        "lab_report_id": item.lab_report_id,
        "has_lab_report": item.has_lab_report,
        "price_source_type": item.price_source_type,
        "image_urls": item.image_urls,
        "extracted_attributes": item.extracted_attributes,
        "validation_tier": validation_result.tier.value,
        "validation_confidence": validation_result.confidence,
        "validation_keep": validation_result.keep,
        "validation_reject_reasons": validation_result.reject_reasons,
        "is_duplicate": item.is_duplicate,
        "duplicate_of": item.duplicate_of,
        "job_id": job_id,
    })

    # Update job stats
    if job_id:
        job = db.get_job(job_id)
        if job:
            if validation_result.keep:
                db.update_job(job_id, {"items_kept": job.get("items_kept", 0) + 1})
            else:
                db.update_job(job_id, {"items_rejected": job.get("items_rejected", 0) + 1})

    logger.info(
        "validate_item_complete",
        item_id=item.item_id,
        tier=validation_result.tier.value,
        keep=validation_result.keep,
    )

    return {
        "item_id": item.item_id,
        "tier": validation_result.tier.value,
        "confidence": validation_result.confidence,
        "keep": validation_result.keep,
        "is_duplicate": item.is_duplicate,
        "reject_reasons": validation_result.reject_reasons,
    }


@shared_task(bind=True, max_retries=2, default_retry_delay=30)
def download_images_task(
    self,
    item_id: str,
    image_urls: list[str],
    domain: str,
) -> dict:
    """
    Download images for an item.

    Args:
        item_id: Item ID
        image_urls: List of image URLs
        domain: Source domain

    Returns:
        Dictionary with download results
    """
    from config.settings import get_settings

    logger.info("download_images_start", item_id=item_id, count=len(image_urls))

    settings = get_settings()
    fetcher = Fetcher()
    storage = get_storage()

    downloaded = []
    failed = []

    for i, url in enumerate(image_urls):
        # Generate storage path
        url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
        ext = Path(url).suffix or ".jpg"
        key = f"images/{domain}/{item_id}/{i:03d}_{url_hash}{ext}"

        # Download
        success, content, error = fetcher.fetch_image(url)

        if not success:
            failed.append({"url": url, "error": error})
            continue

        # Get image info
        try:
            img = Image.open(BytesIO(content))
            width, height = img.size

            # Skip small images
            if width < settings.min_image_resolution or height < settings.min_image_resolution:
                failed.append({
                    "url": url,
                    "error": f"Resolution too low: {width}x{height}",
                })
                continue

            # Compute phash
            phash = compute_phash(img)

            # Store image
            storage.put(key, content, content_type=f"image/{ext.lstrip('.')}")

            downloaded.append({
                "url": url,
                "local_path": key,
                "width": width,
                "height": height,
                "phash": phash,
                "file_size_bytes": len(content),
            })

        except Exception as e:
            failed.append({"url": url, "error": str(e)})

    logger.info(
        "download_images_complete",
        item_id=item_id,
        downloaded=len(downloaded),
        failed=len(failed),
    )

    return {
        "item_id": item_id,
        "downloaded": downloaded,
        "failed": failed,
    }


@shared_task(bind=True, max_retries=3, default_retry_delay=60)
def process_url_task(
    self,
    url: str,
    job_id: Optional[str] = None,
) -> dict:
    """
    Complete pipeline for a single URL.

    Combines fetch, parse, validate, and download into one task.

    Args:
        url: URL to process
        job_id: Optional job ID

    Returns:
        Dictionary with processing results
    """
    logger.info("process_url_start", url=url, job_id=job_id)

    # Fetch
    fetch_result = fetch_page_task.apply(args=[url, job_id]).get()

    if "error" in fetch_result:
        return fetch_result

    # Parse
    parse_result = parse_page_task.apply(
        args=[url, fetch_result["content"], fetch_result["robots_allowed"], job_id]
    ).get()

    if "error" in parse_result or "skip" in parse_result:
        return parse_result

    # Validate
    validate_result = validate_item_task.apply(
        args=[parse_result["item"], job_id]
    ).get()

    # Download images if item is kept
    if validate_result.get("keep"):
        item = parse_result["item"]
        download_images_task.apply_async(
            args=[item["item_id"], item["image_urls"], item["source_site"]],
            countdown=5,  # Slight delay to avoid hammering
        )

    logger.info("process_url_complete", url=url, result=validate_result)

    return {
        "url": url,
        "validation": validate_result,
    }


@shared_task
def cleanup_stale_urls_task() -> dict:
    """
    Cleanup stale URLs that have been pending too long.

    Returns:
        Dictionary with cleanup results
    """
    logger.info("cleanup_stale_urls_start")

    db = get_database()
    # Mark URLs pending for more than 24 hours as failed
    # (Implementation depends on database capabilities)

    logger.info("cleanup_stale_urls_complete")

    return {"status": "complete"}
