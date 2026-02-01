#!/usr/bin/env python3
"""
Submit a URL discovery job.

Usage:
    python -m scripts.submit_discovery --site leibish --seed_url https://leibish.com/diamonds --limit 1000
"""

from datetime import datetime
from uuid import uuid4

import click
import structlog

from config.allowed_sites import get_site_config, get_all_allowed_domains, ALLOWED_SITES
from orchestrator.tasks import discover_urls_task, process_url_task
from storage.database import get_database

# Configure logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)
logger = structlog.get_logger(__name__)


# Seed URLs for each site
SEED_URLS = {
    "leibish": "https://www.leibish.com/natural-fancy-color-diamonds",
    "leibish.com": "https://www.leibish.com/natural-fancy-color-diamonds",
    "jamesallen": "https://www.jamesallen.com/loose-diamonds/all-diamonds/",
    "jamesallen.com": "https://www.jamesallen.com/loose-diamonds/all-diamonds/",
    "bluenile": "https://www.bluenile.com/diamond-search",
    "bluenile.com": "https://www.bluenile.com/diamond-search",
    "sothebys": "https://www.sothebys.com/en/buy/jewels",
    "sothebys.com": "https://www.sothebys.com/en/buy/jewels",
    "christies": "https://www.christies.com/en/auctions",
    "christies.com": "https://www.christies.com/en/auctions",
    "bonhams": "https://www.bonhams.com/department/JEW/",
    "bonhams.com": "https://www.bonhams.com/department/JEW/",
}


def get_domain_from_site(site: str) -> str:
    """Get domain from site name."""
    site_lower = site.lower()
    if ".com" in site_lower:
        return site_lower
    return f"{site_lower}.com"


@click.command()
@click.option(
    "--site",
    "-s",
    required=True,
    help="Site name (e.g., leibish, jamesallen, bluenile)",
)
@click.option(
    "--seed_url",
    "-u",
    default=None,
    help="Custom seed URL (uses default if not provided)",
)
@click.option(
    "--limit",
    "-l",
    default=1000,
    help="Maximum URLs to discover",
)
@click.option(
    "--depth",
    "-d",
    default=3,
    help="Maximum crawl depth",
)
@click.option(
    "--async",
    "async_mode",
    is_flag=True,
    help="Submit as async Celery task",
)
def main(site: str, seed_url: str, limit: int, depth: int, async_mode: bool):
    """Submit a URL discovery job."""

    # Get domain
    domain = get_domain_from_site(site)

    # Check allowlist
    site_config = get_site_config(domain)
    if not site_config:
        click.secho(f"Error: {domain} is not in the allowlist", fg="red")
        click.echo("\nAllowed sites:")
        for d in get_all_allowed_domains():
            click.echo(f"  - {d}")
        return

    # Get seed URL
    if not seed_url:
        seed_url = SEED_URLS.get(site.lower())
        if not seed_url:
            click.secho(f"Error: No default seed URL for {site}", fg="red")
            click.echo("Please provide --seed_url")
            return

    # Create job
    job_id = f"job_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{uuid4().hex[:8]}"

    click.echo(f"\n{'='*60}")
    click.echo(f"Submitting Discovery Job")
    click.echo(f"{'='*60}")
    click.echo(f"Job ID:    {job_id}")
    click.echo(f"Site:      {domain}")
    click.echo(f"Seed URL:  {seed_url}")
    click.echo(f"Limit:     {limit} URLs")
    click.echo(f"Depth:     {depth} levels")
    click.echo(f"Mode:      {'async' if async_mode else 'sync'}")
    click.echo(f"{'='*60}\n")

    # Create job in database
    db = get_database()
    db.create_job({
        "job_id": job_id,
        "domain": domain,
        "status": "pending",
        "config": {
            "seed_url": seed_url,
            "limit": limit,
            "depth": depth,
        },
    })

    if async_mode:
        # Submit as Celery task
        click.echo("Submitting async task...")
        result = discover_urls_task.apply_async(
            args=[seed_url, job_id, depth, limit],
            countdown=0,
        )
        click.echo(f"Task ID: {result.id}")
        click.secho("\n✓ Job submitted successfully", fg="green")
        click.echo(f"\nMonitor progress:")
        click.echo(f"  celery -A orchestrator.celery_app inspect active")
        click.echo(f"  python -m scripts.report_stats")
    else:
        # Run synchronously
        click.echo("Running discovery (this may take a while)...")
        db.update_job(job_id, {"status": "running", "started_at": datetime.utcnow()})

        try:
            result = discover_urls_task(seed_url, job_id, depth, limit)

            db.update_job(job_id, {
                "status": "completed",
                "completed_at": datetime.utcnow(),
            })

            click.secho(f"\n✓ Discovery complete", fg="green")
            click.echo(f"  URLs discovered: {result.get('discovered', 0)}")

            # Show next steps
            click.echo(f"\nNext steps:")
            click.echo(f"  1. Start workers to process URLs:")
            click.echo(f"     docker compose up -d")
            click.echo(f"  2. Export validated items:")
            click.echo(f"     python -m scripts.export_validated --output data/validated_items.jsonl")

        except Exception as e:
            db.update_job(job_id, {
                "status": "failed",
                "error_message": str(e),
            })
            click.secho(f"\n✗ Discovery failed: {e}", fg="red")


if __name__ == "__main__":
    main()
