"""
Celery application configuration.

Sets up Celery with Redis as broker and result backend.
"""

from celery import Celery
from kombu import Queue

from config.settings import get_settings

settings = get_settings()

# Create Celery app
celery_app = Celery(
    "gemstone",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
    include=["orchestrator.tasks"],
)

# Celery configuration
celery_app.conf.update(
    # Task settings
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,

    # Result settings
    result_expires=3600,  # 1 hour
    result_extended=True,

    # Worker settings
    worker_prefetch_multiplier=1,  # One task at a time per worker
    worker_concurrency=4,  # 4 concurrent workers per process

    # Task routing
    task_queues=(
        Queue("default", routing_key="default"),
        Queue("discovery", routing_key="discovery"),
        Queue("fetch", routing_key="fetch"),
        Queue("parse", routing_key="parse"),
        Queue("validate", routing_key="validate"),
        Queue("images", routing_key="images"),
    ),

    task_default_queue="default",
    task_default_exchange="default",
    task_default_routing_key="default",

    task_routes={
        "orchestrator.tasks.discover_urls_task": {"queue": "discovery"},
        "orchestrator.tasks.fetch_page_task": {"queue": "fetch"},
        "orchestrator.tasks.parse_page_task": {"queue": "parse"},
        "orchestrator.tasks.validate_item_task": {"queue": "validate"},
        "orchestrator.tasks.download_images_task": {"queue": "images"},
        "orchestrator.tasks.process_url_task": {"queue": "fetch"},
    },

    # Rate limiting
    task_annotations={
        "orchestrator.tasks.fetch_page_task": {
            "rate_limit": "30/m",  # 30 per minute globally
        },
        "orchestrator.tasks.download_images_task": {
            "rate_limit": "60/m",  # 60 per minute
        },
    },

    # Retry settings
    task_acks_late=True,
    task_reject_on_worker_lost=True,

    # Beat scheduler (optional)
    beat_schedule={
        "cleanup-stale-urls": {
            "task": "orchestrator.tasks.cleanup_stale_urls_task",
            "schedule": 3600.0,  # Every hour
        },
    },
)


def get_celery_app() -> Celery:
    """Get the Celery app instance."""
    return celery_app
