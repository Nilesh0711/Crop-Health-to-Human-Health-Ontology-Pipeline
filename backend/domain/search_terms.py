"""Literal search vocabulary and endpoint formatting shared by retrieval and ranking.

These lexical hints select evidence; they do not establish scientific truth.
"""
from __future__ import annotations
import re
from typing import Any

_TITLE_STOPWORDS = {"a", "an", "and", "the", "of", "for", "in", "on", "to", "with", "from", "by", "at", "is", "are"}

_RETRIEVAL_STOPWORDS = _TITLE_STOPWORDS | {"affects", "causes", "reduces", "increases", "leads", "associated", "has"}

_RELATION_EVIDENCE_TERMS = {
    "associated_with": ("incidence", "severity", "occurrence", "prevalence", "susceptibility"),
    "affects": ("impact", "response", "severity", "quality", "contamination"),
    "increases": ("increased", "higher", "elevated", "enhanced"),
    "reduces": ("reduced", "lower", "decreased", "suppressed"),
    "has_contaminant": ("contamination", "detected", "accumulation", "concentration", "production"),
    "has_nutrient": ("concentration", "content", "composition", "accumulation"),
    "has_trait": ("quality", "composition", "resistance", "trait"),
    "contains": ("content", "composition", "detected", "concentration"),
}

def _retrieval_endpoint(value: object) -> str:
    """Return a concise, literal endpoint phrase for an evidence query."""
    text = " ".join(str(value or "").split())
    # These labels are useful in the graph but rarely occur verbatim in study
    # titles/abstracts. Removing them is a lexical normalization, not a
    # scientific synonym expansion.
    text = re.sub(r"(?i)\b(?:cultivation|production|management|system|rate of)\b", " ", text)
    return " ".join(text.split()) or " ".join(str(value or "").split())

def _retrieval_text(candidate: dict[str, Any]) -> str:
    qualifiers = " ".join(str(value) for value in (candidate.get("qualifiers") or {}).values() if value)
    type_hints = []
    if candidate.get("subject_type") == "PlantDisease":
        type_hints.append("disease")
    if candidate.get("object_type") == "PlantDisease":
        type_hints.append("disease")
    relation_terms = " ".join(_RELATION_EVIDENCE_TERMS.get(str(candidate.get("relation") or ""), ()))
    return (f"{candidate.get('subject', '')} {candidate.get('object', '')} "
            f"{relation_terms} {qualifiers} {' '.join(type_hints)}").strip()
