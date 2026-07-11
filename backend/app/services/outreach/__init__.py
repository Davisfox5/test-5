"""Cold-outreach engine — LINDA-originated 1:1 email campaigns.

Modules:
- ``common``    — pure helpers shared by API + Celery: config validation,
  domain normalization, opt-out detection, send-window/quota math,
  pipeline-status transitions.
- ``drafts``    — per-prospect AI personalization (Sonnet via ModelRouter).
- ``scheduler`` — the sync Celery-side engine: pick due members, send via
  the tenant's Gmail/Outlook OAuth, write audit rows, advance state.
- ``replies``   — sync hooks called from email_ingest when an inbound
  message attributes to an outreach send (reply / bounce / opt-out).
"""
