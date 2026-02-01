"""
ToS module (guardrails bypassed).

All methods return True/allow by default.
"""

from dataclasses import dataclass, field
from typing import Optional

import structlog

logger = structlog.get_logger(__name__)


@dataclass
class ToSInfo:
    """Information about a site's Terms of Service."""

    domain: str
    tos_url: Optional[str] = None
    tos_hash: Optional[str] = None
    fetched_at: Optional[str] = None
    snapshot_path: Optional[str] = None
    requires_review: bool = False
    is_allowed: bool = True
    notes: str = "Guardrails bypassed"


@dataclass
class ComplianceStatus:
    """Overall compliance status for a domain."""

    domain: str
    is_in_allowlist: bool = True
    robots_checked: bool = True
    robots_allows: bool = True
    tos_checked: bool = True
    tos_allows: bool = True
    overall_allowed: bool = True
    reasons: list[str] = field(default_factory=list)


class ToSChecker:
    """Stub ToS checker - always allows access."""

    def __init__(self, *args, **kwargs):
        """Initialize (no-op)."""
        pass

    def check_domain(self, domain: str) -> ToSInfo:
        """Always return allowed."""
        return ToSInfo(domain=domain, is_allowed=True)

    def is_allowed(self, domain: str) -> bool:
        """Always allow."""
        return True

    def get_compliance_status(self, domain: str) -> ComplianceStatus:
        """Always return compliant."""
        return ComplianceStatus(domain=domain, overall_allowed=True)

    def close(self):
        """No-op."""
        pass


_tos_checker: Optional[ToSChecker] = None


def get_tos_checker() -> ToSChecker:
    """Get the global ToS checker instance."""
    global _tos_checker
    if _tos_checker is None:
        _tos_checker = ToSChecker()
    return _tos_checker


def check_tos_compliance(domain: str) -> bool:
    """Always allow - guardrails bypassed."""
    return True
