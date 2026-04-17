"""Transcription service — unified interface for Deepgram and Whisper engines."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

from backend.app.config import get_settings

logger = logging.getLogger(__name__)


@dataclass
class Segment:
    start: float      # seconds
    end: float        # seconds
    text: str
    speaker_id: Optional[str]
    confidence: Optional[float]


class TranscriptionService:
    """Produces diarized transcript segments from an audio file."""

    async def transcribe(
        self,
        audio_path: str,
        engine: str = "deepgram",
        language: str = "en",
        keyterms: Optional[List[str]] = None,
    ) -> List[Segment]:
        """Transcribe an audio file using the specified engine.

        Args:
            audio_path: Local filesystem path to the audio file.
            engine: ``"deepgram"`` (cloud) or ``"whisper"`` (local).
            language: BCP-47 language code, default ``"en"``.
            keyterms: Optional domain-specific keywords to boost recognition.

        Returns:
            Ordered list of :class:`Segment` objects with speaker labels.
        """
        if engine == "deepgram":
            return await self._transcribe_deepgram(audio_path, language, keyterms)
        elif engine == "whisper":
            return await self._transcribe_whisper(audio_path, language)
        else:
            raise ValueError(f"Unknown transcription engine: {engine}")

    # ── Deepgram ─────────────────────────────────────────────────────────

    async def _transcribe_deepgram(
        self,
        audio_path: str,
        language: str,
        keyterms: Optional[List[str]],
    ) -> List[Segment]:
        """Transcribe via Deepgram Nova-3 with diarization."""
        try:
            from deepgram import DeepgramClient, PrerecordedOptions, FileSource

            settings = get_settings()
            client = DeepgramClient(settings.DEEPGRAM_API_KEY)

            audio_bytes = Path(audio_path).read_bytes()
            payload: FileSource = {"buffer": audio_bytes}

            options_kwargs = {
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

            response = client.listen.prerecorded.v("1").transcribe_file(
                payload, options
            )

            return self._parse_deepgram_response(response)

        except ImportError:
            logger.error(
                "deepgram-sdk is not installed. "
                "Install it with: pip install deepgram-sdk"
            )
            raise
        except Exception:
            logger.exception("Deepgram transcription failed for %s", audio_path)
            raise

    @staticmethod
    def _parse_deepgram_response(response) -> List[Segment]:
        """Convert Deepgram JSON response into a flat list of Segments."""
        segments: List[Segment] = []

        # Prefer utterances (already split by speaker turn).
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
        else:
            # Fallback: walk word-level data from the first channel/alternative.
            channels = results.channels
            if channels:
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
                        # Flush accumulated words as a segment.
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

                # Flush remaining.
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

    # ── Whisper (local, via faster-whisper) ───────────────────────────────

    async def _transcribe_whisper(
        self,
        audio_path: str,
        language: str,
    ) -> List[Segment]:
        """Transcribe locally with faster-whisper large-v3.

        Note: faster-whisper is CPU/GPU-bound and synchronous.  We run it in
        the default executor so it doesn't block the event loop.
        """
        try:
            import asyncio
            segments = await asyncio.get_event_loop().run_in_executor(
                None, self._whisper_sync, audio_path, language
            )
            return segments
        except ImportError:
            logger.error(
                "faster-whisper is not installed. "
                "Install it with: pip install faster-whisper"
            )
            raise
        except Exception:
            logger.exception("Whisper transcription failed for %s", audio_path)
            raise

    @staticmethod
    def _whisper_sync(audio_path: str, language: str) -> List[Segment]:
        """Synchronous Whisper transcription helper."""
        from faster_whisper import WhisperModel

        model = WhisperModel("large-v3")
        raw_segments, _info = model.transcribe(
            audio_path,
            language=language,
            word_timestamps=True,
        )

        # TODO: For speaker diarization with Whisper, integrate pyannote-audio
        # (e.g. pyannote.audio Pipeline) and map speaker labels onto these
        # segments by timestamp overlap.  For now speaker_id is left as None.

        results: List[Segment] = []
        for seg in raw_segments:
            results.append(
                Segment(
                    start=seg.start,
                    end=seg.end,
                    text=seg.text.strip(),
                    speaker_id=None,
                    confidence=getattr(seg, "avg_logprob", None),
                )
            )

        return results
