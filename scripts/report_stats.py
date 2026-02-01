#!/usr/bin/env python3
"""
Report statistics from the pipeline.

Usage:
    python -m scripts.report_stats
    python -m scripts.report_stats --input data/validated_items.jsonl
"""

import json
from collections import Counter
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
    "--input",
    "-i",
    "input_path",
    default=None,
    help="Input JSONL file (uses database if not provided)",
)
@click.option(
    "--detailed",
    "-d",
    is_flag=True,
    help="Show detailed statistics",
)
def main(input_path: str, detailed: bool):
    """Report statistics from the pipeline."""

    click.echo(f"\n{'='*60}")
    click.echo(f"Pipeline Statistics")
    click.echo(f"{'='*60}\n")

    if input_path:
        # Load from file
        if not Path(input_path).exists():
            click.secho(f"Error: File not found: {input_path}", fg="red")
            return

        items = []
        with open(input_path, "r", encoding="utf-8") as f:
            for line in f:
                if line.strip():
                    items.append(json.loads(line))

        click.echo(f"Source: {input_path}")
        click.echo(f"Total items: {len(items)}\n")

        # Compute stats
        compute_file_stats(items, detailed)
    else:
        # Load from database
        db = get_database()
        stats = db.get_stats()

        click.echo("Database Statistics:")
        click.echo(f"  Total URLs:      {stats.get('total_urls', 0)}")
        click.echo(f"  Pending URLs:    {stats.get('pending_urls', 0)}")
        click.echo(f"  Total items:     {stats.get('total_items', 0)}")
        click.echo(f"  Kept items:      {stats.get('kept_items', 0)}")

        click.echo("\nItems by tier:")
        for tier, count in sorted(stats.get("items_by_tier", {}).items()):
            click.echo(f"  Tier {tier}: {count}")

        if detailed:
            # Get more detailed stats from items
            items = db.get_validated_items(keep_only=False, limit=10000)
            if items:
                click.echo("\nDetailed Statistics:")
                compute_file_stats(items, detailed)


def compute_file_stats(items: list, detailed: bool):
    """Compute and display statistics from items list."""

    # Basic counts
    total = len(items)
    kept = sum(1 for i in items if i.get("validation_keep"))
    rejected = total - kept
    duplicates = sum(1 for i in items if i.get("is_duplicate"))

    click.echo("Overview:")
    click.echo(f"  Total items:    {total}")
    click.echo(f"  Kept:           {kept} ({100*kept/max(total,1):.1f}%)")
    click.echo(f"  Rejected:       {rejected} ({100*rejected/max(total,1):.1f}%)")
    click.echo(f"  Duplicates:     {duplicates} ({100*duplicates/max(total,1):.1f}%)")

    # By tier
    tier_counts = Counter(i.get("validation_tier") for i in items)
    click.echo("\nBy validation tier:")
    for tier in sorted(tier_counts.keys()):
        count = tier_counts[tier]
        click.echo(f"  Tier {tier}: {count} ({100*count/max(total,1):.1f}%)")

    # By stone type
    stone_counts = Counter(i.get("stone_type_claimed", "unknown") for i in items)
    click.echo("\nBy stone type:")
    for stone, count in stone_counts.most_common(15):
        click.echo(f"  {stone}: {count}")

    # By source
    source_counts = Counter(i.get("source_site", "unknown") for i in items)
    click.echo("\nBy source site:")
    for source, count in source_counts.most_common():
        click.echo(f"  {source}: {count}")

    # Lab reports
    with_lab = sum(1 for i in items if i.get("has_lab_report"))
    click.echo(f"\nLab reports:")
    click.echo(f"  With lab report: {with_lab} ({100*with_lab/max(total,1):.1f}%)")

    # Images
    total_images = sum(len(i.get("image_urls", [])) for i in items)
    avg_images = total_images / max(total, 1)
    click.echo(f"\nImages:")
    click.echo(f"  Total images:   {total_images}")
    click.echo(f"  Avg per item:   {avg_images:.1f}")

    if detailed:
        # Rejection reasons
        all_reasons = []
        for i in items:
            reasons = i.get("validation_reject_reasons", [])
            all_reasons.extend(reasons)

        if all_reasons:
            reason_counts = Counter(all_reasons)
            click.echo("\nTop rejection reasons:")
            for reason, count in reason_counts.most_common(10):
                click.echo(f"  {reason}: {count}")

        # By shape
        shape_counts = Counter(
            i.get("shape", "unknown") for i in items if i.get("shape")
        )
        if shape_counts:
            click.echo("\nBy shape:")
            for shape, count in shape_counts.most_common(10):
                click.echo(f"  {shape}: {count}")

        # Confidence distribution
        confidences = [i.get("validation_confidence", 0) for i in items if i.get("validation_keep")]
        if confidences:
            avg_conf = sum(confidences) / len(confidences)
            click.echo(f"\nConfidence (kept items):")
            click.echo(f"  Average: {avg_conf:.2f}")
            click.echo(f"  High (>0.8): {sum(1 for c in confidences if c > 0.8)}")
            click.echo(f"  Medium (0.5-0.8): {sum(1 for c in confidences if 0.5 <= c <= 0.8)}")
            click.echo(f"  Low (<0.5): {sum(1 for c in confidences if c < 0.5)}")


if __name__ == "__main__":
    main()
