"""Report deterministic text-quality signals without asserting OCR accuracy."""

from __future__ import annotations

import re
from typing import Any


_MOJIBAKE_RE = re.compile(r"(?:Ã.|Â.|â(?:€|€™|€œ|€|€|€)|�)")


_SPLIT_WORD_RE = re.compile(r"\b[a-zA-Z]{2,12}\s+ee\s+[a-zA-Z]{2,12}\b")


_WORD_RE = re.compile(r"\b[\w-]+\b")


def assess_text_quality(text: str) -> dict[str, Any]:
    """Return transparent, non-destructive PDF-text quality signals.

    PDF extraction sometimes turns a word such as ``agricultural`` into
    ``agri ee cultural``.  That does not necessarily justify discarding a
    paper, but it must be visible to the run and to the scope classifier.
    """
    sample = str(text or "")
    words = _WORD_RE.findall(sample)
    mojibake = len(_MOJIBAKE_RE.findall(sample))
    split_words = len(_SPLIT_WORD_RE.findall(sample))
    # A short abstract can still be readable and scientifically useful.  Only
    # treat near-empty extraction as unusable; ordinary short text is handled
    # by the conservative ``uncertain`` scope outcome instead.
    if len(words) < 12:
        quality = "unusable"
    elif mojibake >= 12 or split_words >= 3:
        quality = "degraded"
    else:
        quality = "usable"
    return {
        "detected_quality": quality,
        "word_count": len(words),
        "mojibake_markers": mojibake,
        "split_word_markers": split_words,
    }
