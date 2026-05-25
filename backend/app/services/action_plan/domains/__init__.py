"""Domain templates for Action Plan synthesis.

A ``DomainTemplate`` is a thin shell that gives the synthesizer the
vocabulary and tone for one of the four supported domains. The KB
itself (via the Document Orchestrator) is the load-bearing input —
the template only provides the role framing, tone description, the
customer-endpoint archetype, and example slot vocabulary the
synthesizer can fall back on when the KB doesn't specify.

Adding a new domain = drop a new module under this package that
exposes a ``TEMPLATE`` constant of type ``DomainTemplate`` and
register it in ``REGISTRY``.
"""
from __future__ import annotations

from typing import Dict

from .base import DomainTemplate
from .customer_service import TEMPLATE as CUSTOMER_SERVICE_TEMPLATE
from .generic import TEMPLATE as GENERIC_TEMPLATE
from .it_support import TEMPLATE as IT_SUPPORT_TEMPLATE
from .sales import TEMPLATE as SALES_TEMPLATE


REGISTRY: Dict[str, DomainTemplate] = {
    SALES_TEMPLATE.name: SALES_TEMPLATE,
    CUSTOMER_SERVICE_TEMPLATE.name: CUSTOMER_SERVICE_TEMPLATE,
    IT_SUPPORT_TEMPLATE.name: IT_SUPPORT_TEMPLATE,
    GENERIC_TEMPLATE.name: GENERIC_TEMPLATE,
}


def get(name: str) -> DomainTemplate:
    """Return the domain template for ``name``, falling back to generic.

    A bad / unknown ``name`` never raises so that a typo in tenant
    config or a value the system hasn't seen yet (a future domain
    rolled out partially) degrades to ``generic`` rather than crashing
    plan synthesis.
    """
    return REGISTRY.get(name, GENERIC_TEMPLATE)


__all__ = ["DomainTemplate", "REGISTRY", "get"]
