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


_SCOPE_TERMS = re.compile(
    r"\b(?:soil|crop|plant|root|microb\w*|nutrient\w*|mineral\w*|contamin\w*|pesticid\w*|"
    r"mycotoxin\w*|arsenic|cadmium|food quality|food safety|human health|exposure|disease|"
    r"phytochemical\w*|antioxidant\w*)\b",
    re.IGNORECASE,
)
_FRONT_MATTER = re.compile(
    r"\b(?:open access|correspondence|copyright|creative commons|author contributions|conflict of interest|"
    r"publisher(?:'s|s)? note|received \w+|accepted \w+|published \w+|generative ai statement|"
    r"suggested citation|buy this book|find related titles|all rights reserved|table of contents|"
    r"boxes, figures, and tables|consensus study report)\b",
    re.IGNORECASE,
)


def _looks_like_index_listing(text: str) -> bool:
    return (len(re.findall(r"\b\d{1,2}-\d{1,3}\b", text)) >= 3
            or len(re.findall(r",\s*\d{1,3}\b", text)) >= 6)


def _sample_packets(packets: list[dict[str, Any]], maximum_characters: int = 12_000) -> str:
    """Build a representative scientific sample from across the document.

    Publisher PDFs can devote most of the first pages to metadata. Selecting
    only the first characters caused directly relevant papers to be labelled
    out of scope before their scientific sentences were reached. We rank
    sentences by pathway content, then restore document order for readability.
    """
    ranked: list[tuple[int, int, str, str]] = []
    position = 0
    for packet in packets:
        heading = str(packet.get("section_heading", "Paper text"))
        for item in packet.get("sentences", []):
            text = str(item.get("text", "")).strip()
            if not text or _FRONT_MATTER.search(text) or _looks_like_index_listing(text):
                position += 1
                continue
            score = len(_SCOPE_TERMS.findall(text))
            if score:
                ranked.append((score, position, heading, text))
            position += 1
    ranked.sort(key=lambda item: (-item[0], item[1]))
    selected = sorted(ranked[:48], key=lambda item: item[1])
    parts: list[str] = []
    used = 0
    current_heading = ""
    for _score, _position, heading, sentence in selected:
        prefix = f"[{heading}] " if heading != current_heading else ""
        piece = prefix + sentence
        if used + len(piece) > maximum_characters:
            remaining = maximum_characters - used
            if remaining > 120:
                parts.append(piece[:remaining])
            break
        parts.append(piece)
        used += len(piece) + 1
        current_heading = heading
    return " ".join(parts)


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
