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
from enterprise.engine.base import CompsEngine, IdentifyEngine
from enterprise.engine.remote import RemoteCompsEngine, RemoteIdentifyEngine
from enterprise.engine.sandbox import SandboxEngine
from enterprise.engine.sandbox_identify import SandboxIdentifier
from enterprise.errors import install_error_handlers
from enterprise.models import utcnow
from enterprise.routes import router
from enterprise.settings import EnterpriseSettings, get_settings
from enterprise.valuation.model_versions import ModelRegistry

logger = logging.getLogger(__name__)

API_DESCRIPTION = """
The GemstoneAI Enterprise API: the pricing and verification layer for jewelry.

The **Scan API** is the business version of a GemstoneAI scan result. Send photos of a piece
of jewelry or a loose stone, its details, or both, and get back what it is and what it's worth:
the identification (observations, alternatives, open questions, next evidence), a price range in
your chosen valuation basis, a Raw/Make/Market breakdown for pieces, the comparables behind it
(each tagged as a realized sale or an asking price), a confidence score with reason codes, and
scenario pricing for unknowns such as natural vs lab-grown.

* **Authentication:** `Authorization: Bearer gsk_...` (or `X-API-Key`). Keys are scoped per product.
* **Sandbox:** `gsk_test_` keys run against simulated identification and synthetic comparables;
  responses carry `synthetic_data: true`.
* **No guessing:** an item that can't be identified well enough (`unidentified`), or fewer than 5
  qualifying comparables (`insufficient_data`), means no price.
* **Audit:** every scan is stored and retrievable by `scan_id`.
* **Reproducibility:** every response is stamped with `model_version`; pin one per request or per account.
* **Idempotency:** send `Idempotency-Key` on POSTs to make retries safe.
"""

_UNSET: Any = object()


def live_engine_from_settings(settings: EnterpriseSettings) -> Optional[CompsEngine]:
    if settings.engine_backend == "remote":
        if not settings.engine_url:
            raise RuntimeError(
                "ENTERPRISE_ENGINE_URL is required when ENTERPRISE_ENGINE_BACKEND=remote"
            )
        return RemoteCompsEngine(
            settings.engine_url, settings.engine_api_key, settings.engine_timeout_seconds
        )
    if settings.engine_backend == "sandbox":
        logger.warning(
            "ENTERPRISE_ENGINE_BACKEND=sandbox: live tenants will receive SYNTHETIC prices"
        )
        return SandboxEngine()
    return None


def live_identifier_from_settings(settings: EnterpriseSettings) -> Optional[IdentifyEngine]:
    if settings.identify_backend == "remote":
        if not settings.identify_url:
            raise RuntimeError(
                "ENTERPRISE_IDENTIFY_URL is required when ENTERPRISE_IDENTIFY_BACKEND=remote"
            )
        return RemoteIdentifyEngine(
            settings.identify_url, settings.identify_api_key, settings.identify_timeout_seconds
        )
    if settings.identify_backend == "sandbox":
        logger.warning(
            "ENTERPRISE_IDENTIFY_BACKEND=sandbox: live tenants will receive SIMULATED identification"
        )
        return SandboxIdentifier()
    return None


def create_enterprise_app(
    settings: Optional[EnterpriseSettings] = None,
    *,
    db: Optional[Database] = None,
    live_engine: Optional[CompsEngine] = _UNSET,
    sandbox_engine: Optional[CompsEngine] = None,
    registry: Optional[ModelRegistry] = None,
    clock: Optional[Callable[[], datetime]] = None,
    sandbox_identifier: Optional[IdentifyEngine] = None,
    live_identifier: Optional[IdentifyEngine] = _UNSET,
    sleep: Optional[Callable[[float], None]] = None,
) -> FastAPI:
    settings = settings or get_settings()
    state = AppState(
        settings=settings,
        db=db or Database(settings.database_url),
        registry=registry or ModelRegistry(default_id=settings.default_model_version),
        sandbox_engine=sandbox_engine or SandboxEngine(),
        live_engine=live_engine_from_settings(settings) if live_engine is _UNSET else live_engine,
        sandbox_identifier=sandbox_identifier or SandboxIdentifier(),
        live_identifier=(
            live_identifier_from_settings(settings) if live_identifier is _UNSET else live_identifier
        ),
        clock=clock or utcnow,
    )
    if sleep is not None:
        state.sleep = sleep
    app = FastAPI(
        title="GemstoneAI Enterprise API",
        version=__version__,
        description=API_DESCRIPTION,
        openapi_tags=[
            {"name": "Scan API", "description": "Identify and price jewelry and loose stones."},
            {"name": "Account", "description": "Usage and account information."},
        ],
        openapi_url="/openapi.json",
        docs_url="/docs",
        redoc_url=None,
    )
    app.state.enterprise = state
    install_error_handlers(app)
    app.include_router(router)
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
