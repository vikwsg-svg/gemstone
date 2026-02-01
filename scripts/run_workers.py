#!/usr/bin/env python3
"""
Run Celery workers locally.

Usage:
    python -m scripts.run_workers
    python -m scripts.run_workers --queues fetch,parse --concurrency 4
"""

import subprocess
import sys

import click


@click.command()
@click.option(
    "--queues",
    "-q",
    default="default,discovery,fetch,parse,validate,images",
    help="Comma-separated list of queues to process",
)
@click.option(
    "--concurrency",
    "-c",
    default=4,
    help="Number of worker processes",
)
@click.option(
    "--loglevel",
    "-l",
    default="INFO",
    type=click.Choice(["DEBUG", "INFO", "WARNING", "ERROR"]),
    help="Log level",
)
@click.option(
    "--beat",
    is_flag=True,
    help="Also run the beat scheduler",
)
def main(queues: str, concurrency: int, loglevel: str, beat: bool):
    """Run Celery workers for the gemstone pipeline."""

    click.echo(f"\n{'='*60}")
    click.echo(f"Starting Celery Workers")
    click.echo(f"{'='*60}")
    click.echo(f"Queues:      {queues}")
    click.echo(f"Concurrency: {concurrency}")
    click.echo(f"Log level:   {loglevel}")
    click.echo(f"Beat:        {beat}")
    click.echo(f"{'='*60}\n")

    # Build command
    cmd = [
        sys.executable, "-m", "celery",
        "-A", "orchestrator.celery_app",
        "worker",
        "--queues", queues,
        "--concurrency", str(concurrency),
        "--loglevel", loglevel,
    ]

    if beat:
        cmd.extend(["--beat"])

    click.echo(f"Command: {' '.join(cmd)}\n")

    try:
        # Run Celery worker
        subprocess.run(cmd, check=True)
    except KeyboardInterrupt:
        click.echo("\nShutting down workers...")
    except subprocess.CalledProcessError as e:
        click.secho(f"Worker exited with error: {e}", fg="red")
        sys.exit(1)


if __name__ == "__main__":
    main()
