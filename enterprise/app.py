"""
Application factories.

``create_enterprise_app`` builds the self-contained enterprise API (its own
routes, auth, error format and OpenAPI document). ``create_app`` mounts it at
``/enterprise/v1`` on a root app. To serve it from the main GemstoneAI
backend instead, mount it there the same way:

    backend_app.mount("/enterprise/v1", create_enterprise_app())

Run locally:  uvicorn --factory enterprise.app:create_app --reload
"""

import copy
import logging
from collections.abc import Callable
from datetime import datetime
from typing import Any, Optional

from fastapi import FastAPI

from enterprise import __version__
from enterprise.db import Database
from enterprise.deps import AppState
from enterprise.engine.base import ScanEngine
from enterprise.engine.remote import RemoteScanEngine
from enterprise.engine.sandbox import SandboxScanEngine
from enterprise.errors import install_error_handlers
from enterprise.models import utcnow
from enterprise.routes import router
from enterprise.routes_index import router as index_router
from enterprise.routes_products import router as products_router
from enterprise.settings import EnterpriseSettings, get_settings

logger = logging.getLogger(__name__)

API_DESCRIPTION = """
The GemstoneAI Enterprise API: the pricing and verification layer for jewelry.

* **Scan API** (`/scans`): today's GemstoneAI scan result, enhanced for businesses. Send 1-5 photos
  of a piece of jewelry or a loose stone (plus anything you already know) and get the
  identification, the price context and the comparables behind it, each tagged as an asking price
  or a realized sale.
* **Intake** (`/intake`): the scan plus your routing rules: list, review, lab or decline.
* **Estimate** (`/estimate`): the scan plus a pre-sale estimate and reserve built only from realized
  sales.
* **Verify** (`/verify`): the scan plus risk checks on a listing, claim or appraisal.
* **Index** (`/index`): the GemstoneAI Price Index (GPI) and sub-indices as a monthly feed.

Conventions:

* **Authentication:** `Authorization: Bearer gsk_...` (or `X-API-Key`). Keys are scoped per product.
* **Sandbox:** `gsk_test_` keys return example results; responses carry `synthetic_data: true`.
* **No guessing:** no price with fewer than 5 comparables, or when the item can't be identified;
  the reason is always given.
* **Audit:** every result is stored and retrievable by id, with the scan it was based on.
* **Versioned:** every result is stamped with the scan `model_version`; you can pin one.
* **Idempotency:** send `Idempotency-Key` on POSTs to make retries safe.
"""

_UNSET: Any = object()


def live_engine_from_settings(settings: EnterpriseSettings) -> Optional[ScanEngine]:
    if settings.scan_backend == "remote":
        if not settings.scan_url:
            raise RuntimeError("ENTERPRISE_SCAN_URL is required when ENTERPRISE_SCAN_BACKEND=remote")
        return RemoteScanEngine(settings.scan_url, settings.scan_api_key,
                                settings.scan_timeout_seconds)
    if settings.scan_backend == "sandbox":
        logger.warning("ENTERPRISE_SCAN_BACKEND=sandbox: live tenants will receive EXAMPLE results")
        return SandboxScanEngine()
    return None


def create_enterprise_app(
    settings: Optional[EnterpriseSettings] = None,
    *,
    db: Optional[Database] = None,
    live_engine: Optional[ScanEngine] = _UNSET,
    sandbox_engine: Optional[ScanEngine] = None,
    clock: Optional[Callable[[], datetime]] = None,
    sleep: Optional[Callable[[float], None]] = None,
) -> FastAPI:
    settings = settings or get_settings()
    state = AppState(
        settings=settings,
        db=db or Database(settings.database_url),
        sandbox_engine=sandbox_engine or SandboxScanEngine(),
        live_engine=live_engine_from_settings(settings) if live_engine is _UNSET else live_engine,
        clock=clock or utcnow,
    )
    if sleep is not None:
        state.sleep = sleep
    app = FastAPI(
        title="GemstoneAI Enterprise API",
        version=__version__,
        description=API_DESCRIPTION,
        openapi_tags=[
            {"name": "Scan API", "description": "Scan jewelry and loose stones: identify and price."},
            {"name": "Intake", "description": "Route consigned items: list, review, lab or decline."},
            {"name": "Estimate", "description": "Pre-sale estimates and reserves from realized sales."},
            {"name": "Verify", "description": "Risk signals for listings, claims and appraisals."},
            {"name": "Index", "description": "The GemstoneAI Price Index (GPI) and sub-indices."},
            {"name": "Account", "description": "Usage and account information."},
        ],
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.enterprise = state
    install_error_handlers(app)
    app.include_router(router)
    app.include_router(products_router)
    app.include_router(index_router)
    return app


def create_app(settings: Optional[EnterpriseSettings] = None, **kwargs: Any) -> FastAPI:
    enterprise_app = create_enterprise_app(settings, **kwargs)
    root = FastAPI(title="GemstoneAI", docs_url=None, redoc_url=None, openapi_url=None)

    @root.get("/healthz", include_in_schema=False)
    def healthz() -> dict:
        return {"status": "ok"}

    root.mount("/enterprise/v1", enterprise_app)
    root.state.enterprise_app = enterprise_app
    return root


def export_openapi(enterprise_app: FastAPI, server_url: str) -> dict:
    """The enterprise OpenAPI document with the public server URL set."""
    spec = copy.deepcopy(enterprise_app.openapi())
    spec["servers"] = [{"url": server_url, "description": "Production"}]
    return spec
