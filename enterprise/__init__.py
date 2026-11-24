"""
GemstoneAI Enterprise.

A versioned B2B API (``/enterprise/v1``) for auction houses, luxury resale
platforms, marketplaces and insurers. This package is deliberately isolated
from the consumer and dealer surfaces: it has its own FastAPI app, its own
OpenAPI document, its own auth (per-tenant API keys with product scopes) and
its own tables (all prefixed ``ent_``).

Products: Scan API (identify + price, v1 built here), Intake, Estimate, Verify and
Index (planned; their scopes are reserved in ``enterprise.auth.PRODUCTS``).
"""

__version__ = "1.0.0"
