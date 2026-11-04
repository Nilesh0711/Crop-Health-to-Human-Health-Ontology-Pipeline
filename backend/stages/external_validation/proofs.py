"""Read-time evidence contracts for historical claim-validation records."""

from __future__ import annotations

import re
from typing import Any

from backend.domain.claim_matching import entity_anchored, entity_terms
from backend.domain.alignment import (
    ALIGNMENT_SCHEMA,
    ENTITY_ALIGNMENTS_SCHEMA,
    grounded_alignment,
)
from backend.stages.literature_retrieval.clients import normalize_text


VALID_DECISIONS = {"supported", "contradicted", "insufficient", "needs_review"}
MAX_EVIDENCE_WORDS = 180

_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": sorted(VALID_DECISIONS)},
        "entity_alignment": ENTITY_ALIGNMENTS_SCHEMA,
        "qualifier_alignment": {"type": "object", "additionalProperties": ALIGNMENT_SCHEMA},
        "matches": {
            "type": "object",
            "properties": {
                "subject": {"type": "boolean"},
                "object": {"type": "boolean"},
                "relation": {"type": "boolean"},
                "direction": {"type": "boolean"},
                "certainty": {"type": "boolean"},
            },
            "required": ["subject", "object", "relation", "direction", "certainty"],
            "additionalProperties": False,
        },
        "evidence": {
            "type": "array", "maxItems": 1,
            "items": {
                "type": "object",
                "properties": {
                    "chunk_id": {"type": "string"},
                    "sentence_numbers": {
                        "type": "array", "minItems": 1, "maxItems": 3,
                        "items": {"type": "integer", "minimum": 1},
                    },
                    "subject_mention": {"type": "string"},
                    "object_mention": {"type": "string"},
                    "relation_trigger": {"type": "string"},
                    "stance": {"type": "string", "enum": ["supports", "contradicts"]},
                },
                "required": ["chunk_id", "sentence_numbers", "subject_mention", "object_mention", "relation_trigger", "stance"],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["decision", "matches", "entity_alignment", "qualifier_alignment", "evidence", "rationale"],
    "additionalProperties": False,
}


_GENERIC_ALIAS_TERMS = {
    "approach", "approaches", "method", "methods", "management", "practice", "practices",
    "technique", "techniques", "treatment", "treatments", "system", "systems", "stress",
    "level", "levels", "concentration", "concentrations", "content", "contents",
    "soil", "crop", "plant", "human", "ability", "effect", "effects", "response", "responses",
    "fertilizer", "fertilizers", "fertilisation", "fertilization", "material", "materials",
}


def _concept_terms(value: object) -> set[str]:
    """Normalize a small set of lexical scientific variants, not broad synonyms."""
    text = str(value or "").casefold()
    text = re.sub(r"organic matter derived from fertil\w*", "organic fertilizer", text)
    normalized: set[str] = set()
    for term in entity_terms(text):
        if term.startswith(("microb", "microorgan")):
            normalized.add("microbe")
        elif term.startswith(("fertiliz", "fertilis")):
            normalized.add("fertilizer")
        elif term.startswith("suppress"):
            normalized.add("suppress")
        elif term in {"composition", "community", "communities"}:
            normalized.add("community")
        elif term.startswith("accumul"):
            normalized.add("accumulate")
        elif term.startswith("infect"):
            normalized.add("infect")
        else:
            normalized.add(term)
    return normalized - _GENERIC_ALIAS_TERMS








def _mention_in_quote(value: object, quote: object) -> bool:
    mention = " ".join(str(value or "").casefold().split())
    text = " ".join(str(quote or "").casefold().split())
    return bool(mention) and mention in text


def _alias_compatible(candidate_entity: object, evidence_mention: object) -> bool:
    """Allow only narrow lexical variants after the judge supplied a surface mention.

    This accepts ``mulching techniques`` / ``mulching practices`` but rejects
    the materially different ``organic fertilizer`` / ``suspension fertilizer``.
    It is intentionally not an ontology or semantic-similarity substitute.
    """
    candidate_terms = _concept_terms(candidate_entity)
    evidence_terms = _concept_terms(evidence_mention)
    if not candidate_terms or not evidence_terms:
        return False
    return candidate_terms <= evidence_terms or evidence_terms <= candidate_terms


def _entity_evidence_compatible(candidate_entity: object, evidence_mention: object, quote: object,
                                candidate_aliases: tuple[object, ...] = ()) -> bool:
    if not _mention_in_quote(evidence_mention, quote):
        return False
    alternatives = (candidate_entity, *candidate_aliases)
    return any(
        entity_anchored(str(alternative or ""), str(quote or ""))
        or _alias_compatible(alternative, evidence_mention)
        for alternative in alternatives if str(alternative or "").strip()
    )


def _condition_anchored(value: object, quote: object) -> bool:
    """Match an explicit condition or its unambiguous source-defined initialism."""
    condition = str(value or "").strip()
    if not condition:
        return True
    if entity_anchored(condition, str(quote or "")):
        return True
    words = re.findall(r"[A-Za-z]+", condition)
    initialism = "".join(word[0] for word in words)
    return len(initialism) >= 2 and bool(re.search(rf"(?i)\b{re.escape(initialism)}\b", str(quote or "")))








def decision_errors(packet: dict[str, Any], decision: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    document = next((item for item in packet.get("evidence_documents", []) if item.get("evidence_document_id") == decision.get("evidence_document_id")), None)
    if document is None:
        return ["evidence_document_id is not present in validation packet"]
    if decision.get("decision") not in VALID_DECISIONS:
        errors.append("decision must be supported, contradicted, insufficient, or needs_review")
    candidate = packet.get("candidate") or {}
    if candidate and decision.get("candidate_id") != candidate.get("candidate_id"):
        errors.append("candidate_id does not match the claim")
    evidence = decision.get("evidence", [])
    if not isinstance(evidence, list):
        return errors + ["evidence must be a list"]
    if decision.get("decision") in {"supported", "contradicted"} and not evidence:
        errors.append("supported/contradicted decisions require cited evidence")
    if decision.get("decision") == "supported":
        for field in ("subject_match", "object_match", "relation_match", "direction_match", "certainty_match"):
            if _match_value(decision, field) is not True:
                errors.append(f"{field} must be true for support")
    elif decision.get("decision") == "contradicted":
        for field in ("subject_match", "object_match"):
            if _match_value(decision, field) is not True:
                errors.append(f"{field} must be true for contradiction")
    if decision.get("decision") in {"supported", "contradicted"} and len(evidence) != 1:
        errors.append("decisive evidence must cite one passage")
    chunks = {chunk["chunk_id"]: chunk["text"] for chunk in document.get("evidence_chunks", [])}
    for item in evidence:
        if not isinstance(item, dict):
            errors.append("each evidence item must be an object")
            continue
        text, quote = chunks.get(item.get("chunk_id")), normalize_text(str(item.get("quoted_span", "")))
        if text is None:
            errors.append("evidence chunk_id is not in the cited document")
        elif not quote:
            errors.append("evidence item lacks quoted_span")
        elif quote.casefold() not in normalize_text(text).casefold():
            errors.append("quoted_span is not contained in stored evidence")
        if decision.get("decision") in {"supported", "contradicted"}:
            if len(quote.split()) > MAX_EVIDENCE_WORDS or len(quote.split()) < 3:
                errors.append(f"decisive quote must contain 3-{MAX_EVIDENCE_WORDS} words")
            expected_stance = "supports" if decision["decision"] == "supported" else "contradicts"
            if item.get("stance") != expected_stance:
                errors.append("evidence stance does not match decision")
            trigger = item.get("relation_trigger")
            if trigger is not None and not _mention_in_quote(trigger, quote):
                errors.append("quoted evidence does not contain the stated relation trigger")
            if candidate:
                for field in ("subject", "object"):
                    evidence_mention = item.get(f"{field}_mention")
                    alignments = decision.get("entity_alignment") or {}
                    alignment = alignments.get(field) if isinstance(alignments, dict) else None
                    grounded = (grounded_alignment(alignment, candidate.get(field), quote, text or "")
                                and _mention_in_quote(evidence_mention, quote)
                                and str(alignment.get("mention") or "").casefold() == str(evidence_mention or "").casefold())
                    aliases = (candidate.get(f"{field}_source_mention", ""),)
                    matches_entity = (
                        _entity_evidence_compatible(
                            candidate.get(field, ""), evidence_mention, quote, aliases,
                        )
                        if evidence_mention is not None
                        else entity_anchored(str(candidate.get(field, "")), quote)
                    )
                    if not (grounded if alignment is not None else matches_entity):
                        errors.append(f"quoted evidence does not explicitly name the claim {field}")
                qualifiers = candidate.get("qualifiers") or {}
                for field in ("crop_or_species", "population", "comparator", "metric", "condition"):
                    value = str(qualifiers.get(field, "")).strip()
                    matches_qualifier = (_condition_anchored(value, quote) if field == "condition"
                                         else entity_anchored(value, quote))
                    qualifier_alignments = decision.get("qualifier_alignment") or {}
                    qualifier_alignment = qualifier_alignments.get(field) if isinstance(qualifier_alignments, dict) else None
                    if value and qualifier_alignment is not None:
                        matches_qualifier = grounded_alignment(qualifier_alignment, value, quote, text or "")
                    if value and not matches_qualifier:
                        errors.append(f"quoted evidence does not explicitly match {field}")
    return errors


def _match_value(decision: dict[str, Any], legacy_field: str) -> Any:
    """Read the compact v8 match object while accepting legacy decision files."""
    compact_name = legacy_field.removesuffix("_match")
    matches = decision.get("matches")
    if isinstance(matches, dict) and compact_name in matches:
        return matches[compact_name]
    return decision.get(legacy_field)
