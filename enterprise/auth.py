"""
Per-tenant API keys with per-product scopes.

Key format: ``gsk_<live|test>_<12-char key id>_<32-char secret>``. Only the
SHA-256 of the full key is stored (keys are 190+ bits of randomness, so a
slow KDF adds nothing). The plaintext is shown once, at creation.

Send it as ``Authorization: Bearer <key>`` (preferred) or ``X-API-Key: <key>``.
"""

import hashlib
import hmac
import re
import secrets
import string
from dataclasses import dataclass
from typing import Optional

from fastapi import Depends, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.orm import Session

from enterprise.deps import get_db
from enterprise.errors import ApiError
from enterprise.ids import new_id
from enterprise.models import ApiKey, Tenant

# One scope per product. Only "scan" has endpoints in v1; the rest are reserved
# so keys can be issued for pilots without a schema change.
PRODUCTS = ("scan", "intake", "estimate", "verify", "index")

KEY_PATTERN = re.compile(r"^gsk_(live|test)_([a-z0-9]{12})_([A-Za-z0-9]{32})$")
_ID_ALPHABET = string.ascii_lowercase + string.digits
_SECRET_ALPHABET = string.ascii_letters + string.digits


def hash_key(raw_key: str) -> str:
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _random(alphabet: str, n: int) -> str:
    return "".join(secrets.choice(alphabet) for _ in range(n))


def create_tenant(
    session: Session,
    name: str,
    environment: str = "live",
    pinned_model_version: Optional[str] = None,
) -> Tenant:
    if environment not in ("live", "sandbox"):
        raise ValueError("environment must be 'live' or 'sandbox'")
    tenant = Tenant(
        id=new_id("ten"),
        name=name,
        environment=environment,
        status="active",
        pinned_model_version=pinned_model_version,
    )
    session.add(tenant)
    session.flush()
    return tenant


def issue_api_key(
    session: Session, tenant: Tenant, scopes: list[str], name: str = "default"
) -> tuple[ApiKey, str]:
    """Create a key for ``tenant``. Returns (record, plaintext). Caller commits."""
    unknown = sorted(set(scopes) - set(PRODUCTS))
    if unknown:
        raise ValueError(f"unknown scopes: {unknown}; valid: {list(PRODUCTS)}")
    if not scopes:
        raise ValueError("at least one scope is required")
    env = "live" if tenant.environment == "live" else "test"
    suffix = _random(_ID_ALPHABET, 12)
    raw = f"gsk_{env}_{suffix}_{_random(_SECRET_ALPHABET, 32)}"
    record = ApiKey(
        id=f"key_{suffix}",
        tenant_id=tenant.id,
        name=name,
        key_hash=hash_key(raw),
        scopes=sorted(set(scopes)),
    )
    session.add(record)
    session.flush()
    return record, raw


@dataclass(frozen=True)
class Principal:
    tenant: Tenant
    api_key: ApiKey

    @property
    def scopes(self) -> frozenset[str]:
        return frozenset(self.api_key.scopes or [])


_bearer = HTTPBearer(auto_error=False, description="Enterprise API key as a bearer token.")
_header = APIKeyHeader(
    name="X-API-Key", auto_error=False, description="Alternative to the Authorization header."
)

_UNAUTHENTICATED = {"WWW-Authenticate": "Bearer"}


def authenticate(session: Session, raw_key: Optional[str]) -> Principal:
    if not raw_key:
        raise ApiError(401, "missing_api_key", "An API key is required.", headers=_UNAUTHENTICATED)
    match = KEY_PATTERN.match(raw_key.strip())
    if not match:
        raise ApiError(401, "invalid_api_key", "The API key is not valid.", headers=_UNAUTHENTICATED)
    record = session.get(ApiKey, f"key_{match.group(2)}")
    if (
        record is None
        or not hmac.compare_digest(record.key_hash, hash_key(raw_key.strip()))
        or record.revoked_at is not None
    ):
        raise ApiError(401, "invalid_api_key", "The API key is not valid.", headers=_UNAUTHENTICATED)
    tenant = session.get(Tenant, record.tenant_id)
    if tenant is None or tenant.status != "active":
        raise ApiError(403, "tenant_inactive", "This account is not active.")
    return Principal(tenant=tenant, api_key=record)


def get_principal(
    bearer: Optional[HTTPAuthorizationCredentials] = Security(_bearer),
    header_key: Optional[str] = Security(_header),
    db: Session = Depends(get_db),
) -> Principal:
    raw = bearer.credentials if bearer is not None else header_key
    return authenticate(db, raw)


def require_scope(product: str):
    if product not in PRODUCTS:
        raise ValueError(f"unknown product scope {product!r}")

    def _dependency(principal: Principal = Depends(get_principal)) -> Principal:
        if product not in principal.scopes:
            raise ApiError(
                403,
                "insufficient_scope",
                f"This API key does not have the '{product}' scope.",
                details={"required_scope": product, "key_scopes": sorted(principal.scopes)},
            )
        return principal

    return _dependency
