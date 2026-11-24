"""
Operator CLI (there is no admin console in v1).

    python -m enterprise.cli create-tenant --name "Acme Insurance" --environment sandbox
    python -m enterprise.cli create-key --tenant ten_... --scopes scan --name "prod server"
    python -m enterprise.cli revoke-key --key-id key_...
    python -m enterprise.cli pin-model --tenant ten_... --version scan-2026.09.0
    python -m enterprise.cli export-openapi --output docs/enterprise/openapi.json

Uses ENTERPRISE_DATABASE_URL. Schema changes go through Alembic
(``alembic -c enterprise/alembic.ini upgrade head``); ``init-db`` exists only
for local SQLite development.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

import click

from enterprise.auth import PRODUCTS, create_tenant, issue_api_key
from enterprise.db import Database
from enterprise.models import ApiKey, Tenant
from enterprise.settings import get_settings
from enterprise.valuation.model_versions import ModelRegistry


def _db() -> Database:
    return Database(get_settings().database_url)


@click.group()
def cli() -> None:
    """GemstoneAI Enterprise operator commands."""


@cli.command("init-db")
def init_db() -> None:
    """Create tables directly (local SQLite only)."""
    db = _db()
    if not db.url.startswith("sqlite"):
        raise click.ClickException("init-db is for local SQLite only; use Alembic migrations.")
    Path("data").mkdir(exist_ok=True)
    db.create_all()
    click.echo(f"Created enterprise tables in {db.url}")


@cli.command("create-tenant")
@click.option("--name", required=True)
@click.option("--environment", type=click.Choice(["live", "sandbox"]), default="sandbox",
              show_default=True)
@click.option("--pin", "pin", default=None, help="Pin a model version for this tenant.")
def create_tenant_cmd(name: str, environment: str, pin: str | None) -> None:
    if pin and ModelRegistry().get(pin) is None:
        raise click.ClickException(f"unknown model version {pin!r}")
    with _db().session_scope() as session:
        tenant = create_tenant(session, name, environment, pin)
        session.commit()
        click.echo(json.dumps({"tenant_id": tenant.id, "name": name, "environment": environment,
                               "pinned_model_version": pin}))


@cli.command("create-key")
@click.option("--tenant", "tenant_id", required=True)
@click.option("--scopes", default="scan", show_default=True,
              help=f"Comma-separated, from: {', '.join(PRODUCTS)}")
@click.option("--name", default="default", show_default=True)
def create_key_cmd(tenant_id: str, scopes: str, name: str) -> None:
    with _db().session_scope() as session:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise click.ClickException(f"no tenant {tenant_id}")
        try:
            record, raw = issue_api_key(
                session, tenant, [s.strip() for s in scopes.split(",") if s.strip()], name
            )
        except ValueError as exc:
            raise click.ClickException(str(exc)) from exc
        session.commit()
        click.echo(json.dumps({"key_id": record.id, "scopes": record.scopes, "api_key": raw}))
        click.echo("Store this key now; it cannot be shown again.", err=True)


@cli.command("revoke-key")
@click.option("--key-id", required=True)
def revoke_key_cmd(key_id: str) -> None:
    with _db().session_scope() as session:
        record = session.get(ApiKey, key_id)
        if record is None:
            raise click.ClickException(f"no key {key_id}")
        record.revoked_at = datetime.now(timezone.utc)
        session.commit()
        click.echo(f"Revoked {key_id}")


@cli.command("pin-model")
@click.option("--tenant", "tenant_id", required=True)
@click.option("--version", "version", default=None)
@click.option("--clear", is_flag=True, help="Remove the tenant's pin.")
def pin_model_cmd(tenant_id: str, version: str | None, clear: bool) -> None:
    if bool(version) == clear:
        raise click.ClickException("pass exactly one of --version or --clear")
    if version and ModelRegistry().get(version) is None:
        raise click.ClickException(f"unknown model version {version!r}")
    with _db().session_scope() as session:
        tenant = session.get(Tenant, tenant_id)
        if tenant is None:
            raise click.ClickException(f"no tenant {tenant_id}")
        tenant.pinned_model_version = None if clear else version
        session.commit()
        click.echo(f"{tenant_id} pinned_model_version = {tenant.pinned_model_version}")


@cli.command("export-openapi")
@click.option("--output", type=click.Path(dir_okay=False), default="docs/enterprise/openapi.json",
              show_default=True)
def export_openapi_cmd(output: str) -> None:
    from enterprise.app import create_enterprise_app, export_openapi
    from enterprise.settings import EnterpriseSettings

    settings = EnterpriseSettings(database_url="sqlite://")
    app = create_enterprise_app(settings, live_engine=None, live_identifier=None)
    spec = export_openapi(app, settings.public_base_url)
    Path(output).parent.mkdir(parents=True, exist_ok=True)
    Path(output).write_text(json.dumps(spec, indent=2, sort_keys=False) + "\n")
    click.echo(f"Wrote {output}")


if __name__ == "__main__":
    cli()
