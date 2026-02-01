#!/usr/bin/env python3
"""
Export validated items to JSONL.

Usage:
    python -m scripts.export_validated --output data/validated_items.jsonl
    python -m scripts.export_validated --tier 0 --output data/tier0_items.jsonl
"""

import json
from pathlib import Path

import click
import structlog

from storage.database import get_database

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
    "--output",
    "-o",
    required=True,
    help="Output JSONL file path",
)
@click.option(
    "--tier",
    "-t",
    type=int,
    default=None,
    help="Filter by specific tier (0, 1, 2)",
)
@click.option(
    "--include-rejected",
    is_flag=True,
    help="Include rejected items",
)
@click.option(
    "--include-duplicates",
    is_flag=True,
    help="Include duplicate items",
)
@click.option(
    "--limit",
    "-l",
    type=int,
    default=None,
    help="Maximum items to export",
)
def main(
    output: str,
    tier: int,
    include_rejected: bool,
    include_duplicates: bool,
    limit: int,
):
    """Export validated items to JSONL format."""

    click.echo(f"\n{'='*60}")
    click.echo(f"Exporting Validated Items")
    click.echo(f"{'='*60}")
    click.echo(f"Output:    {output}")
    click.echo(f"Tier:      {tier if tier is not None else 'all'}")
    click.echo(f"Rejected:  {include_rejected}")
    click.echo(f"Dupes:     {include_duplicates}")
    click.echo(f"Limit:     {limit if limit else 'none'}")
    click.echo(f"{'='*60}\n")

    # Get database
    db = get_database()

    # Query items
    items = db.get_validated_items(
        tier=tier,
        keep_only=not include_rejected,
        limit=limit,
    )

    # Filter duplicates if needed
    if not include_duplicates:
        items = [item for item in items if not item.get("is_duplicate")]

    # Create output directory
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Write JSONL
    exported = 0
    with open(output_path, "w", encoding="utf-8") as f:
        for item in items:
            f.write(json.dumps(item, default=str) + "\n")
            exported += 1

    click.secho(f"\n✓ Exported {exported} items to {output}", fg="green")

    # Show summary
    if exported > 0:
        tier_counts = {}
        stone_counts = {}
        for item in items:
            t = item.get("validation_tier", -1)
            tier_counts[t] = tier_counts.get(t, 0) + 1

            s = item.get("stone_type_claimed", "unknown")
            stone_counts[s] = stone_counts.get(s, 0) + 1

        click.echo("\nBy tier:")
        for t in sorted(tier_counts.keys()):
            click.echo(f"  Tier {t}: {tier_counts[t]}")

        click.echo("\nTop stone types:")
        for s, c in sorted(stone_counts.items(), key=lambda x: -x[1])[:10]:
            click.echo(f"  {s}: {c}")


if __name__ == "__main__":
    main()
