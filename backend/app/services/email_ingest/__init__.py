"""Email ingestion — pulls customer-facing email into the interaction pipeline.

Providers live in submodules (:mod:`gmail`, :mod:`graph`); the shared
:mod:`ingest` module does the classification + DB writes so both
providers land on identical data shapes.
"""
