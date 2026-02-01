"""
Orchestrator module for distributed task execution.

Uses Celery with Redis for distributed processing of:
- URL discovery
- Page fetching
- HTML parsing
- Validation
- Image downloading
"""

from orchestrator.celery_app import celery_app
from orchestrator.tasks import (
    discover_urls_task,
    fetch_page_task,
    parse_page_task,
    validate_item_task,
    download_images_task,
    process_url_task,
)

__all__ = [
    "celery_app",
    "discover_urls_task",
    "fetch_page_task",
    "parse_page_task",
    "validate_item_task",
    "download_images_task",
    "process_url_task",
]
