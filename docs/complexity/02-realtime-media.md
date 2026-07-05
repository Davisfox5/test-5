# 02 — Soft-real-time media + live coaching under a latency budget

**Status:** 🟡 Discussing
**Owner:** _tbd_ · **Working doc — evolves as we design.**

> **Pre-launch lens (2026-07).** No live calls, no raw audio, no tenants on the
> telephony paths yet. Everything here is testable only with synthetic frames and
> unit/integration tests — which is also the cheapest moment to fix concurrency
> and protocol-level gaps, before a real SBC or Genesys org is pointed at us.
>
> Provenance: challenge #2 in the [complexity register](README.md) (register +
> challenges 01/04/05 currently live on the `claude/codebase-complexity-analysis-ltnrm4`
> branch). Every claim below was re-verified against the code on `main` (2026-07-05).

---

## 1. The problem in one sentence

Four real-time ingress paths (SIPREC/SRS sidecar, Genesys AudioHook WS,
Twilio/SignalWire/Telnyx Media Streams, UC recording webhooks) feed a live-coaching
loop with a ~3-second cadence, but the hot path mixes three threads over unlocked
shared state, blocks audio ingest on unbounded Praat work, silently drops frames
under overload or reordering, and treats every disconnect — including a transient
one — as end-of-call.

## 2. Why this is hard (not just buggy)

This is a **soft-real-time system living inside a general-purpose async web app**.
The constraints fight each other:

- **Latency budget vs. compute cost.** Live coaching wants a paralinguistic
  snapshot every ~3s; Praat feature extraction takes 0.5–2s and is uncancellable
  C code. Spend too long and you stall ingest; time out too aggressively and the
  feature never lands.
- **Thread soup by construction.** The asyncio event loop (WS receive), the
  Deepgram SDK's callback thread, and the default thread-pool executor (Praat)
  all touch the same window object. Python's GIL makes single ops atomic but
  compound ops (sort-while-iterate, pop-while-append) are real races.
- **Lossy transports with no replay.** Media Streams and AudioHook push frames at
  wall-clock rate; there is no "rewind." Any stall, drop, or disconnect is
  permanent data loss unless we engineer buffering/resume ourselves.
- **Multi-process reality.** The API runs `--workers 2` (`fly.toml:56`).
  WebSocket paths pin to one worker; SIPREC's frame-per-HTTP-POST design does
  not — per-process state silently breaks.

## 3. Decomposition — four interacting sub-problems

### 2a. Cross-thread mutation of the live paralinguistic window (correctness)

The Deepgram SDK invokes transcript handlers **on its own thread**
(`backend/app/api/telephony.py:228–232`, handler at `:340–382`). The handler calls
`window.update_diarization()` (`:375–377`), which appends to, sorts, and reassigns a
plain list with no lock (`backend/app/services/paralinguistics_live.py:114,133–147`);
`_collapse_adjacent_turns` also mutates turn objects in place (`:261–275`).

Concurrent access from two other threads:
- `feed()` on the event loop pops `_diar_turns[0]` (`paralinguistics_live.py:124–129`).
- `maybe_snapshot()` runs on a thread-pool thread (`api/telephony.py:409–411`) and
  iterates the list in `_build_segments` (`paralinguistics_live.py:225–247`).

`list.sort()` with a Python-level `key` lambda yields between bytecodes, so
iterate-during-sort / pop-during-sort are real interleavings. The in-code comment
claiming GIL safety (`api/telephony.py:321–325`) is wrong for compound operations.

**Latent second race:** `_chunks` (a `deque`, `paralinguistics_live.py:105`) is
appended on the event loop (`:122`) and iterated on the executor thread (`:165`).
Today these never overlap **only because** the handler `await`s the executor inline
— the very thing 2b/2d need to remove. A naive latency fix unmasks a
`RuntimeError: deque mutated during iteration`.

### 2b. Backpressure & ingest stalls → silent frame loss (availability)

- **Media Streams** (`api/telephony.py:258–314`): a single `while True` receive
  loop `await`s the Deepgram send (`:271`) **and the Praat snapshot** (`:279–281`,
  executor await at `:409–411`) inline. Every ~3s, a 0.5–2s Praat run stalls audio
  ingest entirely; frames back up in the socket buffer with no depth check, no
  drop counter, no metric. Twilio-family protocols have no pause message — our
  only lever is to keep the loop fast.
- **SIPREC** (`services/telephony/siprec/bridge.py`): `handle_audio` awaits the
  dispatch inline (`:306–310`). The `TranscriptionDispatch` docstring says
  "the bridge serializes frames on a per-session queue" (`:80–86`) — **no such
  queue exists**. Under an async server, concurrent `audio.frame` POSTs can
  complete out of order; the monotonic sequence guard (`:291–294`) then silently
  drops the late-arriving *earlier* frame (DEBUG log only). No gap detection.
- **AudioHook** is the healthiest path: client-initiated pause/resume with
  drop-while-paused (`services/telephony/audiohook/server.py:464–496`). But there
  is no **server-initiated** flow control for when *we* are slow, and a Deepgram
  send failure is swallowed at DEBUG (`api/audiohook.py:120–123`).

### 2c. No mid-call reconnection/resume (data loss / duplication)

- **Media Streams:** on any disconnect — including a transient network blip — the
  `finally` block dispatches batch analysis (`api/telephony.py:290–314`), which
  finalizes the Interaction and **deletes the Redis transcript buffer**
  (`api/websocket.py:769–800`). If the provider reconnects to the same session
  URL, the earlier transcript is gone, a fresh Deepgram connection restarts
  offsets at 0, and the window's `_call_start` re-anchors — the tail of the call
  becomes a second, misaligned interaction.
- **AudioHook:** `seq`/`clientseq` are ack-echo only; the protocol's
  position/resume concept is tracked (`audiohook/protocol.py:11–16`) but nothing
  implements reconnect recovery — a Genesys reconnect is a brand-new session.
- **SIPREC:** per-process in-memory `_sessions` dict (`bridge.py:179`) behind a
  module singleton (`:447–463`). With `--workers 2` (`fly.toml:56`),
  `recording.started` can land on worker A and `audio.frame` POSTs on worker B —
  worker B drops every frame as "unknown session" (`bridge.py:279–286`), at DEBUG.
  **This is a live defect today, not a scaling concern.** Also: state is lost on
  restart mid-call; `idle_timeout_seconds` is stored but never enforced
  (`:172,181` — no reaper), so a lost `recording.stopped` leaks the session
  forever; and `handle_started` releases the lock between the existence check and
  the insert (`:208–239`), so two concurrent `started` deliveries (SRS retries)
  create duplicate `LiveSession` rows and two dispatch sessions.

### 2d. Live-coaching latency budget (performance)

- Snapshot cadence is 3s (`paralinguistics_live.py:100,151–157`); Praat extraction
  has **no timeout anywhere** — not per-call, not per-feature
  (`services/paralinguistics.py` — jitter/shimmer/HNR calls at `:185–198` are
  unbounded, and Praat is C code that cannot be interrupted once started).
- Every snapshot re-decodes the **entire 30s window** and writes a temp WAV to
  disk (`paralinguistics_live.py:159–196`) — ~480 KB PCM + file I/O every 3s per
  concurrent call.
- The publish path opens a **new Redis connection per snapshot**
  (`api/telephony.py:436–445`).
- Because of the inline executor await (2b), a budget overrun isn't just a late
  snapshot — it is an ingest stall. 2b and 2d are two faces of the same defect on
  this path.

## 4. Dependency structure (what unblocks what)

- **2a must land before or with the 2b/2d fix.** Moving the snapshot off the
  receive loop (the 2b/2d fix) is exactly what unmasks the latent `_chunks` race.
  Locking/serialization design and loop-decoupling design are one decision.
- **2b and 2d on the Media Streams path are one mechanical change**: get the
  expensive work out of the receive loop, bound it, and give it a deadline.
- **2c is orthogonal and the largest design surface** (protocol/session-model
  work per ingress path). Its SIPREC slice (multi-worker state, `handle_started`
  race, idle reaper) is independent of the resume question and fixable now.
- The UC webhook path is **not** in scope for 2a/2b/2d — it is not real-time
  (webhook → DB job row → Celery fetch, idempotent, 120s HTTP timeout). It only
  shows up in 2c as the model for durable session state.

## 5. What the original inspection summary got wrong (corrections)

1. **AudioHook does have flow control** (client-initiated pause/resume +
   drop-while-paused). The gap is server-initiated flow control and resume, not
   "no flow control."
2. **SIPREC's bridge does lock its session map** (`asyncio.Lock`, `bridge.py:180`).
   Its real bugs are different: the check-then-act race in `handle_started`, the
   never-enforced idle timeout, and per-process state under `--workers 2`.
3. **The latency problem is worse than "can stall the snapshot budget"**: the
   inline `await run_in_executor` stalls *audio ingest* for the full Praat
   duration every ~3s.
4. The diarization race is three-threaded (event loop + Deepgram thread +
   executor thread), and there's a second, latent deque race that current code
   only avoids by having the blocking bug (see 2a).

## 6. Options per sub-problem

### 2a — thread-safe window

| # | Option | Pros | Cons |
|---|--------|------|------|
| A1 | `threading.Lock` inside every `LiveParalinguisticWindow` method | Smallest diff; window becomes safe regardless of caller threading | Praat holds no lock but snapshot's buffer-read must; lock shared across three threads invites priority inversion if held during decode |
| A2 | **Single-writer**: Deepgram callback never touches the window; it hands turns to the event loop via `loop.call_soon_threadsafe`, and the snapshot thread receives an immutable copy of (chunks, turns) | All mutation on one thread — races impossible by construction; snapshot works on a stable copy; no locks on the hot path | Slightly more plumbing (the handler needs the loop reference); copies cost a little memory per snapshot |
| A3 | Immutable swap: callback builds a new list and atomically rebinds the attribute; readers grab one reference | Lock-free | Only fixes the turns list, not the deque; in-place mutation in `_collapse_adjacent_turns` must also be rewritten; subtle to keep correct |

### 2b — backpressure / keep the receive loop fast

| # | Option | Pros | Cons |
|---|--------|------|------|
| B1 | **Bounded queue + drop-oldest + drop counter**: receive loop only decodes and enqueues; a per-session consumer task feeds Deepgram/window; queue depth capped, drops counted in a metric and logged once per N | Receive loop never blocks on our compute; overload degrades visibly (metric) instead of silently; same pattern reusable for SIPREC dispatch | Adds a task per session; drop policy must be chosen (oldest keeps recency — right for live coaching) |
| B2 | Flow-control where the protocol supports it: implement server-initiated AudioHook pause when the queue is deep | Cleanest for AudioHook — no loss at all | Only AudioHook supports it; Twilio-family and SIPREC still need B1; more protocol code to test |
| B3 | Do nothing structural; just fire-and-forget the snapshot task | Tiny diff | Leaves Deepgram send inline (fine — it's fast) but unbounded snapshot tasks can pile up; doesn't fix SIPREC; no visibility |

(B1 and B2 compose; B2 is an optional add-on for the AudioHook path.)

### 2c — reconnection / session durability

| # | Option | Pros | Cons |
|---|--------|------|------|
| C1 | **Grace-period finalization**: on disconnect, don't dispatch batch analysis immediately — mark the session "disconnected" in Redis with a TTL (e.g. 30–60s); a rejoin to the same session URL re-attaches, rebases diarization offsets by accumulated audio position, and appends to the same transcript buffer; TTL expiry finalizes | Fixes the worst symptom (transient blip = severed call) on all WS paths with one mechanism; no provider cooperation needed; Redis buffer already exists | Rebasing offsets needs an audio-position counter per session; delayed finalization changes UX slightly (call "ends" up to TTL late); needs a sweeper for expiry |
| C2 | Post-hoc stitching: accept the split, but detect "same call, new session" in the batch pipeline and merge interactions | No real-time complexity | Heuristic matching (caller id + time proximity); live coaching still loses context mid-call; two half-analyses may already have fired |
| C3 | Full protocol resume for AudioHook (position-based) on top of C1 | Genesys-native; zero loss on that path | Only AudioHook; most spec-heavy option; pre-launch we have no Genesys org to conformance-test against |
| C4 | SIPREC durability slice only (independent of C1–C3): move `_sessions` to Redis (or route SRS traffic to one worker), fix the `handle_started` check-then-act under the lock, add an idle reaper | Fixes the *live* multi-worker frame-dropping defect + duplicate-session race + session leak; small and self-contained | Doesn't address WS resume at all — it's the "stop the bleeding" piece |

(C4 is needed regardless; the real choice is C1 vs C2 vs C1+C3.)

### 2d — latency budget for Praat

| # | Option | Pros | Cons |
|---|--------|------|------|
| D1 | **In-flight guard + deadline-skip**: never start a snapshot while one is running; run in executor without blocking ingest (per B1); if a snapshot exceeds a budget (e.g. 2.5s), log + metric, skip publishing the stale result, and back off the cadence | No pile-up, bounded staleness, zero risk of stalling ingest; simple to test with a slow fake extractor | The overrunning Praat thread still runs to completion in the pool (uncancellable) — we bound *impact*, not *CPU* |
| D2 | Per-feature timeouts inside the extractor (jitter/shimmer/HNR each get a slice; drop the feature, keep the snapshot) | Degrades gracefully — pitch/energy usually land even when jitter is slow | Praat calls can't actually be interrupted mid-call; a "timeout" can only be checked *between* features, so worst case is still one feature's runtime |
| D3 | Process-pool extraction with hard kill on deadline | Only true cancellation option for C code | Heavy: process spawn per worker, WAV must cross the process boundary, Fly memory footprint; overkill pre-launch |
| D4 | Cheaper snapshots: incremental decode (only new chunks), reuse a rolling PCM buffer, in-memory WAV (`io.BytesIO`) instead of temp file, shared Redis connection | Cuts constant cost of every snapshot ~in half; helps every option above | Doesn't bound Praat itself; complementary, not sufficient alone |

(D1 + D4 compose; D2 composes with both as a refinement; D3 is the escalation if real-world Praat times prove pathological.)

## 7. Open design questions (to decide together)

1. **2a mechanism:** lock (A1) vs single-writer (A2)? A2 is more code but removes
   the class of bug; A1 is the fast patch.
2. **Drop policy + queue bound (B1):** drop-oldest at what depth? (e.g. ~5s of
   audio ≈ 250 frames.) Is a dropped-frames metric + WARN-once-per-session enough
   visibility pre-launch?
3. **Resume ambition (2c):** grace-period re-attach (C1) everywhere, or accept
   split calls for launch (C2) and do C1 later? Is C3 (AudioHook protocol resume)
   worth building before we have a Genesys org to test against?
4. **Finalization delay (if C1):** what TTL is acceptable before a disconnected
   call finalizes? 30s? 60s?
5. **Praat budget (D1):** what's the right per-snapshot deadline, and should
   overruns widen the cadence (adaptive) or just skip?
6. **SIPREC multi-worker (C4):** Redis-backed session state, or pin SRS traffic
   to a single worker via fly routing? Redis is more code; pinning is config but
   leaves the restart-loses-state problem.
7. **Register placement:** the complexity register (README + 01/04/05) lives on
   `claude/codebase-complexity-analysis-ltnrm4`, not `main`. Merge that branch
   first / cherry-pick the register alongside this doc / leave this doc
   standalone until the register lands?

## 8. Chosen approach (agreed 2026-07-05)

- **2a → A2 (single-writer).** The Deepgram callback thread never touches the
  window; it hands turns to the event loop via `call_soon_threadsafe`. All
  mutation happens on the loop thread; the Praat executor thread receives an
  immutable copy of (audio, turns). Races are impossible by construction.
- **2b → B1 (bounded queue + drop-oldest + metrics)** on the Media Streams path;
  the receive loop only decodes and enqueues, a per-session consumer feeds
  Deepgram and the window. Drops are counted in a metric and warned once per
  session. (Server-initiated AudioHook pause deferred — that path already has
  client-side flow control.)
- **2c → C1 + C3 + C4.** Grace-period re-attach on WS disconnect (Redis
  connection-generation + deferred finalization + offset rebase) across WS
  paths; AudioHook position-based resume on top; SIPREC session/sequence state
  moves to Redis (fixes the `--workers 2` frame-dropping defect, the
  `handle_started` check-then-act race via atomic claim, and enforces the idle
  timeout via TTL).
- **2d → D1 + D4.** In-flight guard, per-snapshot deadline with skip + cadence
  backoff; decode-at-feed (incremental), in-memory WAV, shared Redis connection.
  Per-feature timeouts (D2) and process-pool kill (D3) deferred until real Praat
  timings justify them.

## 9. Implementation increments

Each increment: red tests first (synthetic frames — no live audio exists
pre-launch) → implement → verify.

1. **Single-writer `LiveParalinguisticWindow` (2a).** Split snapshot into a
   loop-thread `begin` (cadence check + immutable copy) and a pure executor-side
   compute; Deepgram handler posts turns through `call_soon_threadsafe`.
   Tests: cross-thread hammer, copy-isolation semantics.
2. **Media Streams loop restructure (2b + 2d).** Bounded frame queue with
   drop-oldest + drop counter; consumer task; snapshot task with in-flight
   guard, deadline-skip, cadence backoff; decode-at-feed; `io.BytesIO` WAV;
   one Redis connection per session. Tests: slow-extractor stall test, overflow
   drop test, deadline-skip test.
3. **Grace-period re-attach (C1).** Connection-generation key in Redis;
   disconnect defers `_dispatch_batch_analysis` behind a TTL (~45s); re-attach
   to the same session URL cancels finalization and rebases diarization offsets
   from the accumulated audio position; clean `stop` finalizes immediately.
   Tests: drop-and-rejoin → one interaction with continuous offsets; TTL expiry
   → finalized.
4. **AudioHook position resume (C3).** Persist per-session audio position on
   close; a re-open for the same AudioHook session id restores position and
   applies the same grace-period finalization. Tests: replayed session fixtures
   with a mid-stream reconnect.
5. **SIPREC durability (C4).** Session map + per-label sequence state in Redis
   with TTL-enforced idle timeout; atomic `SET NX` claim in `handle_started`;
   lazy per-worker dispatch open on first frame; reaper finalizes DB rows for
   expired sessions. Tests: two-bridge (two-worker) simulation, concurrent
   `started` retries, idle expiry.
6. **Wrap-up.** Full suite, `python3 -c "from backend.app.main import app"`,
   doc status flip to 🔵/✅.

**Also noted during verification:** production `get_bridge()` never wires a real
`TranscriptionDispatch` — SIPREC audio currently flows to `_NullDispatch` and is
discarded (bridge.py:450–463). Pre-launch by design, but it means increment 5's
"lazy dispatch open" is the natural place to wire Deepgram when SIPREC goes live.
