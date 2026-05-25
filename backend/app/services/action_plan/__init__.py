"""Action Plan synthesis, execution, and domain templates.

Public surface lives in submodules; this package's ``__init__`` is
intentionally near-empty to keep import-time side effects minimal and
to avoid circular imports between the engine and the synthesizer.

Submodules:

* ``domains``          - per-domain template registry (sales / CS / IT / generic).
* ``prompts``          - prompt templates for Calls A / B / C / D.
* ``synthesizer``      - produces a plan from a transcript + KB + brief (Calls A/B/C).
* ``extractor``        - Call D + RFC 822 inbound-email matcher.
* ``engine``           - state machine, debounced regen, completion cascades.
"""
