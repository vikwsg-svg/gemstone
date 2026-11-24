import pytest
from fastapi.testclient import TestClient

from enterprise.app import create_app
from enterprise.auth import create_tenant, issue_api_key
from enterprise.db import Database
from enterprise.settings import EnterpriseSettings
from tests.enterprise.helpers import Clock, FakeEngine, TenantKey


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def db(tmp_path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'enterprise.db'}")
    database.create_all()
    return database


@pytest.fixture
def settings(db) -> EnterpriseSettings:
    return EnterpriseSettings(_env_file=None, database_url=db.url, engine_backend="none")


@pytest.fixture
def live_engine() -> FakeEngine:
    return FakeEngine()


@pytest.fixture
def app(db, settings, live_engine, clock):
    return create_app(settings, db=db, live_engine=live_engine, clock=clock)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def make_tenant(db):
    def _make(name="Acme Insurance", environment="live", scopes=("comps",), pin=None) -> TenantKey:
        with db.session_scope() as s:
            tenant = create_tenant(s, name, environment, pin)
            record, raw = issue_api_key(s, tenant, list(scopes))
            s.commit()
            return TenantKey(tenant.id, raw, record.id)

    return _make


@pytest.fixture
def tenant(make_tenant) -> TenantKey:
    return make_tenant()
