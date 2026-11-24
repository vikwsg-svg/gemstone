"""Shared application state and FastAPI dependencies."""

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from fastapi import Request
from sqlalchemy.orm import Session

from enterprise.db import Database
from enterprise.engine.base import CompsEngine
from enterprise.models import utcnow
from enterprise.settings import EnterpriseSettings
from enterprise.valuation.model_versions import ModelRegistry


@dataclass
class AppState:
    settings: EnterpriseSettings
    db: Database
    registry: ModelRegistry
    sandbox_engine: CompsEngine
    live_engine: Optional[CompsEngine]
    clock: Callable[[], datetime] = field(default=utcnow)


def get_state(request: Request) -> AppState:
    return request.app.state.enterprise


def get_db(request: Request) -> Iterator[Session]:
    session = get_state(request).db.session()
    try:
        yield session
    finally:
        session.close()
