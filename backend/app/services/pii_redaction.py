"""PII redaction service — detects and masks sensitive data in transcripts.

Uses Microsoft Presidio for entity recognition and anonymization.
Falls back gracefully (returning text unchanged) if presidio or spacy
is not installed.
"""

from __future__ import annotations

import copy
import logging
from typing import Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Default entity types to detect.
DEFAULT_ENTITIES: List[str] = [
    "CREDIT_CARD",
    "EMAIL_ADDRESS",
    "PHONE_NUMBER",
    "US_SSN",
    "PERSON",
    "LOCATION",
    "DATE_TIME",
    "IP_ADDRESS",
    "URL",
    "US_BANK_NUMBER",
    "US_PASSPORT",
    "US_DRIVER_LICENSE",
]

# Flag indicating whether presidio + spacy are available.
_PRESIDIO_AVAILABLE = True

try:
    from presidio_analyzer import AnalyzerEngine
    from presidio_anonymizer import AnonymizerEngine
    from presidio_anonymizer.entities import OperatorConfig
except ImportError:
    _PRESIDIO_AVAILABLE = False
    logger.warning(
        "presidio-analyzer / presidio-anonymizer not installed. "
        "PII redaction will be disabled (text returned unchanged). "
        "Install with: pip install presidio-analyzer presidio-anonymizer"
    )


class PIIRedactionService:
    """Detect and redact PII using Microsoft Presidio."""

    def __init__(self) -> None:
        self._analyzer: Optional[object] = None
        self._anonymizer: Optional[object] = None

        if not _PRESIDIO_AVAILABLE:
            return

        # Ensure the spacy model is available.
        try:
            import spacy
            try:
                spacy.load("en_core_web_sm")
            except OSError:
                logger.info("Downloading spacy model en_core_web_sm ...")
                import spacy.cli
                spacy.cli.download("en_core_web_sm")
        except ImportError:
            logger.warning(
                "spacy is not installed. PII redaction will be disabled. "
                "Install with: pip install spacy"
            )
            return

        self._analyzer = AnalyzerEngine()
        self._anonymizer = AnonymizerEngine()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def redact(
        self,
        text: str,
        config: Optional[Dict] = None,
    ) -> Tuple[str, List[Dict]]:
        """Redact PII entities from *text*.

        Parameters
        ----------
        text:
            The input string to scan.
        config:
            Optional configuration dict.  If it contains an ``entities``
            key, that list of entity type strings is used instead of the
            default set.

        Returns
        -------
        tuple[str, list[dict]]
            ``(redacted_text, redactions)`` where each redaction dict has
            keys ``entity_type``, ``start``, ``end``, and
            ``original_length``.
        """
        if self._analyzer is None or self._anonymizer is None:
            logger.debug("Presidio not available — returning text unchanged.")
            return text, []

        entities = DEFAULT_ENTITIES
        if config and "entities" in config:
            entities = config["entities"]

        # Analyse the text for PII.
        results = self._analyzer.analyze(  # type: ignore[union-attr]
            text=text,
            entities=entities,
            language="en",
        )

        if not results:
            return text, []

        # Build operator config: replace each entity with <ENTITY_TYPE>.
        operators: Dict[str, OperatorConfig] = {}
        for r in results:
            operators[r.entity_type] = OperatorConfig(
                "replace",
                {"new_value": f"<{r.entity_type}>"},
            )

        # Record redaction metadata *before* anonymisation (offsets shift).
        redactions: List[Dict] = []
        for r in results:
            redactions.append({
                "entity_type": r.entity_type,
                "start": r.start,
                "end": r.end,
                "original_length": r.end - r.start,
            })

        anonymized = self._anonymizer.anonymize(  # type: ignore[union-attr]
            text=text,
            analyzer_results=results,
            operators=operators,
        )

        return anonymized.text, redactions

    def redact_segments(
        self,
        segments: List[Dict],
        config: Optional[Dict] = None,
    ) -> List[Dict]:
        """Redact PII in a list of transcript segment dicts.

        Each segment is expected to have at least a ``text`` key.  All
        other fields are preserved.  A ``redacted: True`` flag is added
        to every returned segment.

        Parameters
        ----------
        segments:
            List of transcript segment dicts (e.g. from transcription).
        config:
            Optional PII redaction configuration (forwarded to
            :meth:`redact`).

        Returns
        -------
        list[dict]
            New list of segment dicts with redacted text.
        """
        redacted_segments: List[Dict] = []
        for seg in segments:
            new_seg = copy.deepcopy(seg)
            original_text = new_seg.get("text", "")
            redacted_text, _redactions = self.redact(original_text, config=config)
            new_seg["text"] = redacted_text
            new_seg["redacted"] = True
            redacted_segments.append(new_seg)
        return redacted_segments
