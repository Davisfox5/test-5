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
from typing import Any, List, Optional

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
    ) -> List[Segment]:
        """Transcribe audio using the specified engine.

        Exactly one of ``audio_path`` or ``audio_url`` must be provided.
        URL mode is only supported by the Deepgram engine.
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
    ) -> List[Segment]:
        if engine == "deepgram":
            return await self._transcribe_deepgram(
                audio_path=audio_path,
                audio_url=audio_url,
                language=language,
                keyterms=keyterms,
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
    ) -> List[Segment]:
        """Transcribe via Deepgram Nova-3 with native diarization."""
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

        client = DeepgramClient(settings.DEEPGRAM_API_KEY)
        options_kwargs: dict[str, Any] = {
            "model": "nova-3",
            "diarize": True,
            "language": language,
            "smart_format": True,
            "utterances": True,
            "punctuate": True,
        }
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
        from faster_whisper import WhisperModel

        model = WhisperModel("large-v3")
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


def _run_pyannote_diarization(audio_path: str) -> List[_DiarTurn]:
    """Run pyannote speaker-diarization-3.1 on the audio and return a flat
    list of ``(start, end, speaker)`` turns.

    Returns an empty list if the pipeline cannot be loaded or the audio
    file is unusable — callers then leave ``speaker_id=None``.
    """
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
