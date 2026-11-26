"""Shared application state and FastAPI dependencies."""

import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from fastapi import Request
import httpx
from sqlalchemy.orm import Session

from enterprise.db import Database
from enterprise.engine.base import ScanEngine
from enterprise.models import utcnow
from enterprise.settings import EnterpriseSettings


@dataclass
class AppState:
    settings: EnterpriseSettings
    db: Database
    sandbox_engine: ScanEngine
    live_engine: Optional[ScanEngine]
    clock: Callable[[], datetime] = field(default=utcnow)
    sleep: Callable[[float], None] = field(default=time.sleep)
    # Tests inject an httpx.MockTransport; production uses the default network transport.
    webhook_transport: Optional[httpx.BaseTransport] = None


def get_state(request: Request) -> AppState:
    return request.app.state.enterprise


def get_db(request: Request) -> Iterator[Session]:
    session = get_state(request).db.session()
    try:
        yield session
    finally:
        session.close()
