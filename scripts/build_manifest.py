#!/usr/bin/env python3
"""
Build training manifest from validated items.

Usage:
    python -m scripts.build_manifest --input data/validated_items.jsonl --output data/train_manifest.csv
"""

import csv
import json
from pathlib import Path
from uuid import uuid4

import click
import structlog

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
    required=True,
    help="Input JSONL file with validated items",
)
@click.option(
    "--output",
    "-o",
    required=True,
    help="Output CSV manifest file",
)
@click.option(
    "--min-tier",
    type=int,
    default=0,
    help="Minimum tier to include (default: 0)",
)
@click.option(
    "--max-tier",
    type=int,
    default=2,
    help="Maximum tier to include (default: 2)",
)
@click.option(
    "--require-images",
    is_flag=True,
    default=True,
    help="Only include items with downloaded images",
)
def main(
    input_path: str,
    output: str,
    min_tier: int,
    max_tier: int,
    require_images: bool,
):
    """Build training manifest from validated items."""

    click.echo(f"\n{'='*60}")
    click.echo(f"Building Training Manifest")
    click.echo(f"{'='*60}")
    click.echo(f"Input:        {input_path}")
    click.echo(f"Output:       {output}")
    click.echo(f"Tier range:   {min_tier} - {max_tier}")
    click.echo(f"Req. images:  {require_images}")
    click.echo(f"{'='*60}\n")

    # Read input
    if not Path(input_path).exists():
        click.secho(f"Error: Input file not found: {input_path}", fg="red")
        return

    items = []
    with open(input_path, "r", encoding="utf-8") as f:
        for line in f:
            if line.strip():
                items.append(json.loads(line))

    click.echo(f"Loaded {len(items)} items")

    # Create output directory
    output_path = Path(output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    # Build manifest
    manifest_rows = []
    stats = {
        "total": 0,
        "filtered_tier": 0,
        "filtered_images": 0,
        "included": 0,
    }

    for item in items:
        stats["total"] += 1

        # Filter by tier
        tier = item.get("validation_tier")
        if tier is None or tier < min_tier or tier > max_tier:
            stats["filtered_tier"] += 1
            continue

        # Get images
        downloaded = item.get("downloaded_images", [])
        if require_images and not downloaded:
            # Try image_urls as fallback
            image_urls = item.get("image_urls", [])
            if not image_urls:
                stats["filtered_images"] += 1
                continue
            # Use URLs as paths (will need to be downloaded)
            images = [{"local_path": url, "url": url} for url in image_urls]
        else:
            images = downloaded

        # Create rows for each image
        item_id = item.get("item_id", str(uuid4()))
        stone_type = item.get("stone_type_claimed", "unknown")
        confidence = item.get("validation_confidence", 0.0)
        source_url = item.get("source_url", "")

        for i, img in enumerate(images):
            image_path = img.get("local_path") or img.get("url", "")
            if not image_path:
                continue

            manifest_rows.append({
                "stone_id": f"{item_id}_{i:03d}",
                "image_path": image_path,
                "label": stone_type,
                "tier": tier,
                "confidence": confidence,
                "source_url": source_url,
            })

        stats["included"] += 1

    # Write CSV
    if manifest_rows:
        fieldnames = ["stone_id", "image_path", "label", "tier", "confidence", "source_url"]

        with open(output_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(manifest_rows)

    # Summary
    click.echo("\nStatistics:")
    click.echo(f"  Total items:          {stats['total']}")
    click.echo(f"  Filtered (tier):      {stats['filtered_tier']}")
    click.echo(f"  Filtered (no images): {stats['filtered_images']}")
    click.echo(f"  Included items:       {stats['included']}")
    click.echo(f"  Total rows:           {len(manifest_rows)}")

    # Label distribution
    if manifest_rows:
        label_counts = {}
        tier_counts = {}
        for row in manifest_rows:
            label = row["label"]
            tier = row["tier"]
            label_counts[label] = label_counts.get(label, 0) + 1
            tier_counts[tier] = tier_counts.get(tier, 0) + 1

        click.echo("\nBy label:")
        for label, count in sorted(label_counts.items(), key=lambda x: -x[1]):
            click.echo(f"  {label}: {count}")

        click.echo("\nBy tier:")
        for tier in sorted(tier_counts.keys()):
            click.echo(f"  Tier {tier}: {tier_counts[tier]}")

    click.secho(f"\n✓ Manifest written to {output}", fg="green")
    click.echo(f"  {len(manifest_rows)} rows total")


if __name__ == "__main__":
    main()
