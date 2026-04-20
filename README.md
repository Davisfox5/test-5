# LINDA

**Listening Intelligence and Natural Dialogue Assistant** — a call-intelligence
platform for sales and support teams. Linda listens to every customer call,
extracts the moments that matter, coaches reps in real time, and follows up
afterward.

## Layout

- `backend/` — FastAPI service (Postgres + Celery + Redis + Anthropic)
- `website/` — vanilla HTML/CSS/JS marketing page (`index.html`) and
  interactive demo dashboard (`demo.html`)
- `tests/` — pytest suite
- `seed_cs.py`, `seed_it.py`, `seed_sales.py`, `backend/seed.py` — demo data
- `backend/analyze_seed.py` — re-runs the real AI pipeline over seeded
  interactions to refresh insights

## Running

```bash
pip install -r requirements.txt
uvicorn backend.app.main:app --reload
```

For the static demo UI, `python server.py` serves `website/` on port 8000.
