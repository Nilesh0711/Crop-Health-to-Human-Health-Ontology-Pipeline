"""Document-level scope and text-quality checks for the evidence pipeline.

The check is deliberately conservative: it can stop a paper only when the
sample clearly contains no usable soil-to-human-health evidence.  A paper
containing only an upstream mechanism is retained as ``upstream_only`` so it
can still contribute an independently evidenced bridge later.
"""

from __future__ import annotations

import json
import re
from typing import Any

from external_validator.llm import OllamaClient
from .ontology_schema import PATHWAY_DESCRIPTION


_SCOPE_SCHEMA = {
    "type": "object",
    "properties": {
        "scope": {
            "type": "string",
            "enum": ["in_scope", "upstream_only", "out_of_scope", "uncertain"],
        },
        "text_quality": {"type": "string", "enum": ["usable", "degraded", "unusable"]},
        "rationale": {"type": "string"},
    },
    "required": ["scope", "text_quality", "rationale"],
    "additionalProperties": False,
}

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


def _sample_packets(packets: list[dict[str, Any]], maximum_characters: int = 8_000) -> str:
    """Build a compact, representative title/abstract/early-methods sample."""
    parts: list[str] = []
    remaining = maximum_characters
    for packet in packets[:3]:
        text = " ".join(str(item.get("text", "")) for item in packet.get("sentences", []))
        if not text:
            continue
        heading = str(packet.get("section_heading", "Paper text"))
        piece = f"[{heading}] {text}"
        parts.append(piece[:remaining])
        remaining -= len(parts[-1])
        if remaining <= 0:
            break
    return "\n\n".join(parts)


def classify_document_scope(title: str, packets: list[dict[str, Any]], client: OllamaClient) -> dict[str, Any]:
    """Classify a paper before expensive claim extraction.

    ``out_of_scope`` is reserved for papers that are clearly about a mapping,
    economics, infrastructure, trade, generic food security, or yield without
    a usable biological/chemical pathway claim.  Uncertainty never suppresses
    extraction.
    """
    sample = _sample_packets(packets)
    quality = assess_text_quality(sample)
    if quality["detected_quality"] == "unusable":
        return {
            "scope": "uncertain",
            "text_quality": "unusable",
            "rationale": "Too little readable text was available for a safe scope decision.",
            "quality_signals": quality,
        }

    system = (
        "Classify whether one scientific paper can contribute an evidence-backed edge to the stated "
        "soil-to-human-health knowledge-graph pathway. Use only the title and sampled source text. "
        "Choose in_scope for direct pathway evidence, upstream_only for real agriculture/climate/soil/crop/disease "
        "mechanisms that may later bridge downstream, out_of_scope only when the paper is principally mapping, "
        "statistics, economics, infrastructure, trade, generic food security, or yield-only and has no usable "
        "biological/chemical pathway evidence, and uncertain when the sample cannot establish this. "
        "Never mark a paper out_of_scope merely because it does not reach human health directly. "
        "Also rate the readability of supplied text. Return JSON only."
    )
    payload = {
        "project_pathway": PATHWAY_DESCRIPTION,
        "paper_title": title,
        "text_quality_signals": quality,
        "sampled_source_text": sample,
    }
    judged = client.chat_json(system, json.dumps(payload, ensure_ascii=False), max_tokens=260,
                              json_schema=_SCOPE_SCHEMA)
    model_quality = str(judged.get("text_quality", "usable"))
    detected_quality = quality["detected_quality"]
    # Only transparent, deterministic extraction signals may terminate a run.
    # A generative model can flag a readable passage as "unusable" because it
    # finds the scientific wording difficult; treating that as an OCR failure
    # would recreate the false-positive stop that this gate is meant to avoid.
    # Its quality opinion can still downgrade the run to reviewable/degraded.
    if detected_quality == "unusable":
        final_quality = "unusable"
    elif detected_quality == "degraded" or model_quality in {"degraded", "unusable"}:
        final_quality = "degraded"
    else:
        final_quality = "usable"
    return {
        "scope": str(judged.get("scope", "uncertain")),
        "text_quality": final_quality,
        "model_text_quality": model_quality,
        "rationale": str(judged.get("rationale", "")).strip()[:600],
        "quality_signals": quality,
    }
