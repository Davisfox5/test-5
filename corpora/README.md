# Paralinguistic regression corpora

Ground-truth clips we run `paralinguistics_live` against before
promoting the `paralinguistic_live` feature flag to production.

## Manifest format

See `example.yaml` in this directory. Each clip entry points at a
WAV fixture (path resolved relative to the manifest file) and the
timeline of alerts the human annotator expects LINDA to fire.

## How to run

```bash
# Human summary
python -m backend.app.services.paralinguistics_corpus \
    corpora/2026-04-baseline.yaml

# Machine-readable, gated by F1 threshold
python -m backend.app.services.paralinguistics_corpus \
    corpora/2026-04-baseline.yaml \
    --min-f1 0.70 \
    --json
```

Exit code 0 when `overall_f1 >= --min-f1`, 1 otherwise — suitable
for a CI gate before flipping the tenant flag.

## Collection guidelines

- **Length**: 30 s – 3 min per clip. Long enough for the scanner to
  have something to fire on, short enough to stay cheap.
- **Redaction**: any customer audio must be redacted of PII before
  it lands in the repo. Easiest path: regenerate with a TTS engine
  at roughly the same cadence.
- **Label consensus**: two annotators agree on the alert kind and
  time. Tolerance is stored on the expectation so drift by ±5 s
  still counts as a match.
- **Kind coverage**: aim for at least 5 clips per alert kind
  (`monotone`, `pace`, `stress`, `silence`) so per-kind F1 has
  enough signal.
