"""AudioHook recorded-session fixtures for Stream 4 tests.

Each fixture is a Python list of ``(direction, frame)`` tuples
representing one full WebSocket session. ``direction`` is ``"in"``
(client → server) or ``"out"`` (server → client). ``frame`` is
either ``{"text": str}`` for control messages or
``{"bytes": bytes}`` for audio payloads.

The replay harness in ``tests/test_audiohook_server.py`` feeds the
``in`` frames to the state machine and asserts the matching ``out``
frames are emitted in order. Channel layout, media format, pause /
resume timing, and the surrounding session id are all encoded in
the fixture so each test reads as one self-contained scenario.
"""
