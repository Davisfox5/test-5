# LINDA

**Listening Intelligence and Natural Dialogue Assistant** — a call-intelligence
platform for sales and support teams. Linda listens to every customer call,
extracts the moments that matter, coaches reps in real time, and follows up
afterward.

## Layout

- `backend/` — FastAPI service (Postgres + Celery + Redis + Anthropic)
- `apps/app/` — Next.js SPA for Tier 2/3 customers
- `website/` — vanilla HTML/CSS/JS marketing page (`index.html`) and
  interactive demo dashboard (`demo.html`)
- `tests/` — pytest suite
- `docs/` — architecture, business plan, pricing models, scoring spec
- `backend/scripts/seed_{cs,it,sales}.py` (data blobs) + `backend/seed.py`
  (runner) — demo data
- `backend/analyze_seed.py` — re-runs the real AI pipeline over seeded
  interactions to refresh insights

## Running

```bash
pip install -r requirements.txt
uvicorn backend.app.main:app --reload
```

For the static demo UI:

```bash
python -m http.server 8000 --directory website
```
