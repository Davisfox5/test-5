"""Transcription service — unified interface for Deepgram and Whisper engines.

Produces ordered :class:`Segment` objects with speaker labels. Two engines:

* ``deepgram`` — cloud batch ASR with native diarization. Accepts either a
  local file path or a remote URL (``transcribe_url``) so URL-based ingest
  from external recording systems (MiaRec, Dubber, etc.) goes straight to
  the provider without a round-trip through our storage.
* ``whisper`` — self-hosted faster-whisper + pyannote.audio diarization.
  Diarization runs as a second pass and is merged into Whisper segments
  by timestamp overlap.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional

from backend.app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class Segment:
    start: float      # seconds
    end: float        # seconds
    text: str
    speaker_id: Optional[str]
    confidence: Optional[float]


# ── Module-level pyannote pipeline cache ─────────────────────────────────
# Loading pyannote is expensive (model download + torch init). We cache one
# instance per process — Celery workers reuse it across tasks.
_diarization_pipeline: Any = None
_diarization_pipeline_loaded = False


# ── Module-level model caches ────────────────────────────────────────────
#
# faster-whisper's ``WhisperModel("large-v3")`` is ~3 GB. Loading it per
# task adds 5-15s of cold-start. Cache it the same way pyannote already is
# below. Worker process holds the model for the lifetime of the process;
# eager preloading is done from the celery ``worker_process_init`` warmup
# when ``LINDA_WORKER_WARMUP=1``.

_whisper_model: Any = None
_whisper_model_loaded: bool = False


def _get_whisper_model() -> Any:
    """Return a cached faster-whisper ``WhisperModel`` instance."""
    global _whisper_model, _whisper_model_loaded
    if _whisper_model_loaded:
        return _whisper_model
    _whisper_model_loaded = True
    from faster_whisper import WhisperModel

    device = "cpu"
    compute_type = "int8"
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            device = "cuda"
            compute_type = "float16"
    except Exception:
        pass

    settings = get_settings()
    model_size = getattr(settings, "WHISPER_MODEL_SIZE", "large-v3") or "large-v3"
    try:
        _whisper_model = WhisperModel(
            model_size, device=device, compute_type=compute_type
        )
    except Exception:
        # Fall back to a CPU-friendly compute_type if int8 isn't supported.
        logger.exception(
            "Failed to load Whisper %s on %s; retrying with default compute_type",
            model_size, device,
        )
        _whisper_model = WhisperModel(model_size, device=device)
    logger.info("Whisper model %s loaded on %s", model_size, device)
    return _whisper_model


def _get_diarization_pipeline() -> Any:
    """Return a cached pyannote speaker-diarization pipeline, or None if
    the library isn't installed or the HuggingFace token isn't configured.

    The first call downloads the model (~1 GB) from HuggingFace; subsequent
    calls are free. We swallow import + auth errors and return None so the
    caller can decide whether to degrade (no diarization) or raise.
    """
    global _diarization_pipeline, _diarization_pipeline_loaded
    if _diarization_pipeline_loaded:
        return _diarization_pipeline

    _diarization_pipeline_loaded = True  # cache even a None result
    settings = get_settings()
    token = settings.HUGGINGFACE_TOKEN
    if not token:
        logger.warning(
            "HUGGINGFACE_TOKEN not set; Whisper transcripts will have no speaker labels"
        )
        return None

    try:
        from pyannote.audio import Pipeline  # type: ignore
    except ImportError:
        logger.warning(
            "pyannote.audio not installed; Whisper transcripts will have no speaker labels"
        )
        return None

    try:
        pipeline = Pipeline.from_pretrained(
            settings.PYANNOTE_DIARIZATION_MODEL,
            use_auth_token=token,
        )
    except Exception:
        logger.exception(
            "Failed to load pyannote pipeline %s",
            settings.PYANNOTE_DIARIZATION_MODEL,
        )
        return None

    # GPU when available — pyannote is CPU-usable but ~10x slower.
    try:
        import torch  # type: ignore

        if torch.cuda.is_available():
            pipeline.to(torch.device("cuda"))
    except Exception:
        pass

    _diarization_pipeline = pipeline
    return pipeline


class TranscriptionService:
    """Produces diarized transcript segments from an audio file or URL."""

    async def transcribe(
        self,
        audio_path: Optional[str] = None,
        *,
        audio_url: Optional[str] = None,
        engine: str = "deepgram",
        language: str = "en",
        keyterms: Optional[List[str]] = None,
        model: Optional[str] = None,
        tenant_features: Optional[Dict[str, Any]] = None,
    ) -> List[Segment]:
        """Transcribe audio using the specified engine.

        Exactly one of ``audio_path`` or ``audio_url`` must be provided.
        URL mode is only supported by the Deepgram engine.

        ``model`` is an optional engine-specific model override; takes
        precedence over the tenant's ``features_enabled["deepgram_model"]``.
        Deepgram defaults to Nova-2 when neither is set.

        ``tenant_features`` carries the tenant's ``features_enabled`` dict.
        Controls Deepgram premium flags (``deepgram_diarize`` defaults on,
        ``deepgram_smart_format`` / ``deepgram_utterances`` /
        ``deepgram_punctuate`` default off) and the model override.
        """
        if not audio_path and not audio_url:
            raise ValueError("Either audio_path or audio_url must be provided")
        if audio_path and audio_url:
            raise ValueError("Pass audio_path OR audio_url, not both")

        import time as _time

        mode = "url" if audio_url else "file"
        _t0 = _time.monotonic()
        try:
            segments = await self._dispatch(
                audio_path=audio_path,
                audio_url=audio_url,
                engine=engine,
                language=language,
                keyterms=keyterms,
                model=model,
                tenant_features=tenant_features,
            )
        except Exception as exc:
            self._emit_failure_metric(engine, exc)
            raise
        else:
            self._emit_success_metric(engine, mode, _time.monotonic() - _t0, segments)
            return segments

    async def _dispatch(
        self,
        *,
        audio_path: Optional[str],
        audio_url: Optional[str],
        engine: str,
        language: str,
        keyterms: Optional[List[str]],
        model: Optional[str] = None,
        tenant_features: Optional[Dict[str, Any]] = None,
    ) -> List[Segment]:
        if engine == "deepgram":
            return await self._transcribe_deepgram(
                audio_path=audio_path,
                audio_url=audio_url,
                language=language,
                keyterms=keyterms,
                model=model,
                tenant_features=tenant_features,
            )
        if engine == "whisper":
            if audio_url:
                raise ValueError("whisper engine requires a local audio_path")
            return await self._transcribe_whisper(audio_path, language)
        raise ValueError(f"Unknown transcription engine: {engine}")

    # ── Metric emission ─────────────────────────────────────────────────

    @staticmethod
    def _emit_success_metric(
        engine: str, mode: str, elapsed: float, segments: List[Segment]
    ) -> None:
        try:
            from backend.app.services.metrics import (
                TRANSCRIPTION_AUDIO_SECONDS,
                TRANSCRIPTION_SECONDS,
            )

            TRANSCRIPTION_SECONDS.labels(engine=engine, mode=mode).observe(elapsed)
            if segments:
                audio_seconds = max(0.0, segments[-1].end - segments[0].start)
                TRANSCRIPTION_AUDIO_SECONDS.labels(engine=engine).inc(audio_seconds)
        except Exception:
            logger.debug("transcription metric emission failed", exc_info=True)

    @staticmethod
    def _emit_failure_metric(engine: str, exc: Exception) -> None:
        try:
            from backend.app.services.metrics import TRANSCRIPTION_FAILURES

            # Coarse bucketing so the label space stays bounded — full
            # messages live in the Sentry event instead.
            msg = (str(exc) or "").lower()
            if "timeout" in msg or "timed out" in msg:
                reason = "timeout"
            elif "auth" in msg or "401" in msg or "403" in msg:
                reason = "auth"
            elif any(code in msg for code in ("500", "502", "503", "504")):
                reason = "server"
            else:
                reason = "other"
            TRANSCRIPTION_FAILURES.labels(engine=engine, reason=reason).inc()
        except Exception:
            logger.debug("transcription failure metric emission failed", exc_info=True)

    # ── Deepgram ─────────────────────────────────────────────────────────

    async def _transcribe_deepgram(
        self,
        *,
        audio_path: Optional[str],
        audio_url: Optional[str],
        language: str,
        keyterms: Optional[List[str]],
        model: Optional[str] = None,
        tenant_features: Optional[Dict[str, Any]] = None,
    ) -> List[Segment]:
        """Transcribe via Deepgram with native diarization.

        Defaults to Nova-2 (cheaper, ~equivalent accuracy on conversational
        speech). Tenants who need maximum accuracy on hard audio can opt in
        to Nova-3 via ``tenant.features_enabled["deepgram_model"] =
        "nova-3"``. Premium flags (``smart_format``, ``utterances``,
        ``punctuate``, ``diarize``) are tenant-opt-in so AI-only pipelines
        don't pay for human-readable formatting they never consume.
        """
        try:
            from deepgram import DeepgramClient, PrerecordedOptions, FileSource, UrlSource
        except ImportError:
            logger.error(
                "deepgram-sdk is not installed. Install with: pip install deepgram-sdk"
            )
            raise

        settings = get_settings()
        if not settings.DEEPGRAM_API_KEY:
            raise RuntimeError("DEEPGRAM_API_KEY is not configured")

        features = tenant_features or {}
        tenant_model_override = features.get("deepgram_model")
        # Whitelist allowed values to keep API surface predictable.
        # Order: explicit call-site override → tenant feature → cheapest default.
        candidate = model or tenant_model_override or "nova-2"
        chosen_model = candidate if candidate in {"nova-3", "nova-2"} else "nova-2"

        client = DeepgramClient(settings.DEEPGRAM_API_KEY)
        options_kwargs: dict[str, Any] = {
            "model": chosen_model,
            "language": language,
        }
        # Diarization defaults ON (most callers use it) but tenants who
        # only run AI-side analysis can set ``deepgram_diarize=False``.
        if bool(features.get("deepgram_diarize", True)):
            options_kwargs["diarize"] = True
        # smart_format / utterances / punctuate are premium add-ons that
        # produce human-readable text; AI-only downstream pipelines don't
        # need them. Default off; tenants opt in per feature.
        if bool(features.get("deepgram_smart_format", False)):
            options_kwargs["smart_format"] = True
        if bool(features.get("deepgram_utterances", False)):
            options_kwargs["utterances"] = True
        if bool(features.get("deepgram_punctuate", False)):
            options_kwargs["punctuate"] = True
        if keyterms:
            options_kwargs["keywords"] = keyterms
        options = PrerecordedOptions(**options_kwargs)

        try:
            if audio_url:
                source: UrlSource = {"url": audio_url}
                response = client.listen.prerecorded.v("1").transcribe_url(
                    source, options
                )
            else:
                audio_bytes = Path(audio_path).read_bytes()  # type: ignore[arg-type]
                payload: FileSource = {"buffer": audio_bytes}
                response = client.listen.prerecorded.v("1").transcribe_file(
                    payload, options
                )
        except Exception:
            logger.exception(
                "Deepgram transcription failed for %s",
                audio_url or audio_path,
            )
            raise

        return self._parse_deepgram_response(response)

    @staticmethod
    def _parse_deepgram_response(response) -> List[Segment]:
        """Convert Deepgram JSON response into a flat list of Segments."""
        segments: List[Segment] = []

        results = response.results
        utterances = getattr(results, "utterances", None)

        if utterances:
            for utt in utterances:
                segments.append(
                    Segment(
                        start=float(utt.start),
                        end=float(utt.end),
                        text=utt.transcript.strip(),
                        speaker_id=str(utt.speaker) if utt.speaker is not None else None,
                        confidence=float(utt.confidence) if utt.confidence is not None else None,
                    )
                )
            return segments

        # Fallback: walk word-level data from the first channel/alternative.
        channels = results.channels
        if not channels:
            return segments

        alt = channels[0].alternatives[0]
        words = getattr(alt, "words", []) or []
        current_speaker: Optional[str] = None
        buf: List[str] = []
        seg_start: float = 0.0
        seg_end: float = 0.0
        confidences: List[float] = []

        for w in words:
            spk = str(w.speaker) if getattr(w, "speaker", None) is not None else None
            if spk != current_speaker and buf:
                segments.append(
                    Segment(
                        start=seg_start,
                        end=seg_end,
                        text=" ".join(buf).strip(),
                        speaker_id=current_speaker,
                        confidence=(
                            sum(confidences) / len(confidences)
                            if confidences
                            else None
                        ),
                    )
                )
                buf = []
                confidences = []
                seg_start = float(w.start)

            if not buf:
                seg_start = float(w.start)
            current_speaker = spk
            buf.append(w.punctuated_word if hasattr(w, "punctuated_word") else w.word)
            seg_end = float(w.end)
            if w.confidence is not None:
                confidences.append(float(w.confidence))

        if buf:
            segments.append(
                Segment(
                    start=seg_start,
                    end=seg_end,
                    text=" ".join(buf).strip(),
                    speaker_id=current_speaker,
                    confidence=(
                        sum(confidences) / len(confidences)
                        if confidences
                        else None
                    ),
                )
            )
        return segments

    # ── Whisper (local, via faster-whisper) + pyannote diarization ───────

    async def _transcribe_whisper(
        self,
        audio_path: str,
        language: str,
    ) -> List[Segment]:
        """Transcribe locally with faster-whisper, then attach speaker
        labels from a pyannote diarization pass.

        Both faster-whisper and pyannote are synchronous / CPU-bound, so
        we run them in a thread-pool executor.
        """
        import asyncio

        try:
            return await asyncio.get_event_loop().run_in_executor(
                None, self._whisper_sync, audio_path, language
            )
        except ImportError:
            logger.error(
                "faster-whisper is not installed. Install with: pip install faster-whisper"
            )
            raise
        except Exception:
            logger.exception("Whisper transcription failed for %s", audio_path)
            raise

    @staticmethod
    def _whisper_sync(audio_path: str, language: str) -> List[Segment]:
        """Synchronous Whisper transcription + diarization merge."""
        model = _get_whisper_model()
        raw_segments, _info = model.transcribe(
            audio_path,
            language=language,
            word_timestamps=True,
        )

        whisper_segments: List[Segment] = []
        for seg in raw_segments:
            whisper_segments.append(
                Segment(
                    start=float(seg.start),
                    end=float(seg.end),
                    text=seg.text.strip(),
                    speaker_id=None,
                    confidence=getattr(seg, "avg_logprob", None),
                )
            )

        diar_turns = _run_pyannote_diarization(audio_path)
        if diar_turns:
            return _merge_diarization_into_segments(whisper_segments, diar_turns)
        return whisper_segments


# ── Diarization helpers ──────────────────────────────────────────────────


@dataclass
class _DiarTurn:
    start: float
    end: float
    speaker: str


_DIARIZATION_CACHE_TTL_SECONDS = 14 * 24 * 3600  # 2 weeks — long enough to cover redrives


def _audio_content_hash(audio_path: str) -> Optional[str]:
    """SHA-256 of the audio bytes, used as the diarization cache key.

    Returns ``None`` if the file can't be read — caller falls back to a
    fresh diarization pass.
    """
    import hashlib

    try:
        h = hashlib.sha256()
        with open(audio_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        return h.hexdigest()
    except OSError:
        return None


def _diarization_cache_key(audio_hash: str) -> Optional[str]:
    """Tenant-scoped cache key, or ``None`` when no tenant context is bound.

    Keying on audio bytes alone let two tenants who upload byte-identical
    audio share cached speaker labels (cross-tenant leak, 4c audit). The
    pipeline binds the RLS tenant context before transcription, so the
    tenant is in scope here; a path that hasn't bound one gets no caching
    rather than a shared bucket.
    """
    from backend.app.tenant_ctx import get_current_tenant_id

    tenant_id = get_current_tenant_id()
    if tenant_id is None:
        return None
    return "diarization:{0}:{1}".format(tenant_id, audio_hash)


def _diarization_cache_get(audio_hash: str) -> Optional[List[_DiarTurn]]:
    try:
        key = _diarization_cache_key(audio_hash)
        if key is None:
            return None
        import json as _json
        import redis as _redis_lib  # type: ignore

        r = _redis_lib.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
        raw = r.get(key)
        if not raw:
            return None
        return [
            _DiarTurn(start=t["start"], end=t["end"], speaker=t["speaker"])
            for t in _json.loads(raw)
        ]
    except Exception:
        logger.debug("diarization cache read failed", exc_info=True)
        return None


def _diarization_cache_set(audio_hash: str, turns: List[_DiarTurn]) -> None:
    try:
        key = _diarization_cache_key(audio_hash)
        if key is None:
            return
        import json as _json
        import redis as _redis_lib  # type: ignore

        r = _redis_lib.Redis.from_url(get_settings().REDIS_URL, decode_responses=True)
        r.set(
            key,
            _json.dumps(
                [{"start": t.start, "end": t.end, "speaker": t.speaker} for t in turns]
            ),
            ex=_DIARIZATION_CACHE_TTL_SECONDS,
        )
    except Exception:
        logger.debug("diarization cache write failed", exc_info=True)


def _vad_has_speech(audio_path: str, min_speech_fraction: float = 0.02) -> bool:
    """Cheap energy-based VAD: return False when the file is essentially
    silence / hold music, so we can skip pyannote entirely.

    Uses pydub (already a dep) to chunk the audio and look at the RMS
    distribution. ``min_speech_fraction`` is the share of the recording
    that needs to exceed the silence threshold; below it we treat the
    audio as non-speech.

    The 2% floor is deliberately low so a hold-heavy support call (40
    min of music plus a 1-min real exchange = 2.5% speech) still gets
    diarized. The intent is to skip pyannote only on recordings that
    are *all* music / silence — not on real calls with long pauses.
    """
    try:
        from pydub import AudioSegment, silence  # type: ignore
    except ImportError:
        return True

    try:
        audio = AudioSegment.from_file(audio_path)
    except Exception:
        return True  # let pyannote try its luck

    duration_ms = len(audio)
    if duration_ms < 1500:
        return True  # too short to bother filtering

    try:
        nonsilent = silence.detect_nonsilent(
            audio,
            # 500ms chunks; -45 dBFS roughly matches a quiet call's noise floor.
            min_silence_len=500,
            silence_thresh=-45.0,
        )
    except Exception:
        return True

    speech_ms = sum(end - start for start, end in (nonsilent or []))
    if duration_ms <= 0:
        return True
    speech_fraction = speech_ms / duration_ms
    if speech_fraction < min_speech_fraction:
        logger.info(
            "VAD: skipping pyannote — speech_fraction=%.3f below threshold %.3f",
            speech_fraction, min_speech_fraction,
        )
        return False
    return True


def _run_pyannote_diarization(audio_path: str) -> List[_DiarTurn]:
    """Run pyannote speaker-diarization-3.1 on the audio and return a flat
    list of ``(start, end, speaker)`` turns.

    Results are keyed by the SHA-256 of the audio bytes and cached in
    Redis for 2 weeks, so a redrive of the same interaction reuses the
    prior diarization (a single pyannote pass is ~10-30 s of CPU on a
    typical worker, and redrives previously paid this cost every time).

    A cheap VAD pre-filter (pydub RMS chunks) short-circuits on audio
    files that are essentially silence / hold-music, so we don't pay the
    full pyannote pass on recordings that have no speakers to diarize.

    Returns an empty list if the pipeline cannot be loaded or the audio
    file is unusable — callers then leave ``speaker_id=None``.
    """
    audio_hash = _audio_content_hash(audio_path)
    if audio_hash is not None:
        cached = _diarization_cache_get(audio_hash)
        if cached is not None:
            return cached

    if not _vad_has_speech(audio_path):
        # Cache the empty result too so a redrive doesn't re-run VAD.
        if audio_hash is not None:
            _diarization_cache_set(audio_hash, [])
        return []

    pipeline = _get_diarization_pipeline()
    if pipeline is None:
        return []
    try:
        annotation = pipeline(audio_path)
    except Exception:
        logger.exception("pyannote diarization failed for %s", audio_path)
        return []
    turns: List[_DiarTurn] = []
    for segment, _track, speaker in annotation.itertracks(yield_label=True):
        turns.append(
            _DiarTurn(
                start=float(segment.start),
                end=float(segment.end),
                speaker=str(speaker),
            )
        )
    if audio_hash is not None and turns:
        _diarization_cache_set(audio_hash, turns)
    return turns


def _merge_diarization_into_segments(
    segments: List[Segment], diar_turns: List[_DiarTurn]
) -> List[Segment]:
    """Attach a speaker_id to each Whisper segment by picking the diar
    turn with the largest temporal overlap.

    When a segment spans a speaker change (common: turn-taking mid-
    sentence in Whisper output), we leave the segment intact and label it
    with the speaker that covers the most of its duration. The downstream
    ``_parse_deepgram_response`` already produces speaker-clean utterances
    on the Deepgram side, so we mirror that shape here without splitting.
    """
    if not diar_turns:
        return segments

    labeled: List[Segment] = []
    for seg in segments:
        best_speaker: Optional[str] = None
        best_overlap = 0.0
        for turn in diar_turns:
            overlap = max(0.0, min(seg.end, turn.end) - max(seg.start, turn.start))
            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = turn.speaker
        labeled.append(
            Segment(
                start=seg.start,
                end=seg.end,
                text=seg.text,
                speaker_id=best_speaker,
                confidence=seg.confidence,
            )
        )
    return labeled


__all__ = [
    "Segment",
    "TranscriptionService",
]
