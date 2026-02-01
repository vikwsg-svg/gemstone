#!/usr/bin/env python3
"""
Check compliance for a domain.

Usage:
    python -m scripts.check_compliance --domain leibish.com
"""

import sys
from urllib.parse import urlparse

import click
import structlog

from config.allowed_sites import get_site_config, get_all_allowed_domains, ALLOWED_SITES
from crawler.robots import RobotsChecker
from crawler.tos import ToSChecker

# Configure logging
structlog.configure(
    processors=[
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.dev.ConsoleRenderer(),
    ]
)
logger = structlog.get_logger(__name__)


@click.command()
@click.option(
    "--domain",
    "-d",
    required=True,
    help="Domain to check (e.g., leibish.com)",
)
@click.option(
    "--verbose",
    "-v",
    is_flag=True,
    help="Verbose output",
)
def main(domain: str, verbose: bool):
    """Check compliance for a domain before crawling."""

    # Normalize domain
    domain = domain.lower().strip()
    if domain.startswith("http"):
        domain = urlparse(domain).netloc
    if domain.startswith("www."):
        domain = domain[4:]

    click.echo(f"\n{'='*60}")
    click.echo(f"Compliance Check: {domain}")
    click.echo(f"{'='*60}\n")

    # Check allowlist
    click.echo("1. Checking allowlist...")
    site_config = get_site_config(domain)

    if site_config:
        click.secho(f"   ✓ Domain is in allowlist", fg="green")
        click.echo(f"   - Purposes: {', '.join(site_config.purposes)}")
        click.echo(f"   - Expected tier: {site_config.expected_validation_tier}")
        click.echo(f"   - Crawl delay: {site_config.crawl_delay}s")
        click.echo(f"   - Max concurrent: {site_config.max_concurrent}")

        if site_config.trust_lab_reports:
            click.echo("   - Trusts: lab reports")
        if site_config.trust_stone_type:
            click.echo("   - Trusts: stone type")
        if site_config.trust_shape:
            click.echo("   - Trusts: shape")
        if site_config.trust_carat:
            click.echo("   - Trusts: carat")

        if site_config.notes:
            click.echo(f"   - Notes:")
            for note in site_config.notes:
                click.echo(f"     • {note}")
    else:
        click.secho(f"   ✗ Domain NOT in allowlist", fg="red")
        click.echo("\n   Allowed domains:")
        for allowed in get_all_allowed_domains():
            click.echo(f"   - {allowed}")
        click.echo("\n   Cannot proceed with crawling for non-allowed domains.")
        sys.exit(1)

    # Check robots.txt
    click.echo("\n2. Checking robots.txt...")
    robots = RobotsChecker()
    test_url = f"https://{domain}/"

    try:
        can_fetch = robots.can_fetch(test_url)
        crawl_delay = robots.get_crawl_delay(test_url)
        sitemaps = robots.get_sitemaps(test_url)

        if can_fetch:
            click.secho(f"   ✓ robots.txt allows crawling", fg="green")
        else:
            click.secho(f"   ✗ robots.txt denies crawling", fg="red")

        if crawl_delay:
            click.echo(f"   - Crawl-delay: {crawl_delay}s")

        if sitemaps and verbose:
            click.echo(f"   - Sitemaps found: {len(sitemaps)}")
            for sitemap in sitemaps[:5]:
                click.echo(f"     • {sitemap}")

    except Exception as e:
        click.secho(f"   ! Error checking robots.txt: {e}", fg="yellow")

    # Check ToS
    click.echo("\n3. Checking ToS...")
    tos = ToSChecker()
    tos_info = tos.check_domain(domain)

    if tos_info.is_allowed:
        click.secho(f"   ✓ ToS pre-approved (in allowlist)", fg="green")
    else:
        click.secho(f"   ! ToS requires manual review", fg="yellow")
        if tos_info.tos_url:
            click.echo(f"   - ToS URL: {tos_info.tos_url}")
        if tos_info.snapshot_path:
            click.echo(f"   - Snapshot: {tos_info.snapshot_path}")

    # Summary
    click.echo(f"\n{'='*60}")
    click.echo("Summary")
    click.echo(f"{'='*60}")

    all_clear = site_config is not None
    if all_clear:
        click.secho("\n✓ Domain is approved for crawling", fg="green", bold=True)
        click.echo(f"\nNext steps:")
        click.echo(f"  1. Submit discovery job:")
        click.echo(f"     python -m scripts.submit_discovery --site {domain.split('.')[0]} --limit 1000")
        click.echo(f"  2. Start workers:")
        click.echo(f"     docker compose up -d")
        click.echo(f"     OR")
        click.echo(f"     python -m scripts.run_workers")
    else:
        click.secho("\n✗ Domain NOT approved for crawling", fg="red", bold=True)

    click.echo("")


if __name__ == "__main__":
    main()
