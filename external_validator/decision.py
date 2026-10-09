"""Evidence-bound Qwen entailment decisions and invariant checks."""

from __future__ import annotations

import re
from typing import Any

from .claim_matching import entity_anchored
from .llm import OllamaClient
from .retrieval import normalize_text


VALID_DECISIONS = {"supported", "contradicted", "insufficient", "needs_review"}

RELATION_CUES = {
    "affects": ("affect", "impact", "influence", "reduce", "decrease", "increase", "loss", "damage"),
    "causes": ("cause", "caused", "causing", "induce", "induced", "lead to", "result in"),
    "reduces": ("reduce", "reduced", "reducing", "decrease", "decreased", "decreasing", "lower", "loss"),
    "increases": ("increase", "increased", "increasing", "elevate", "raised", "higher"),
    "leads_to": ("lead to", "led to", "result in", "resulted in", "consequence"),
    "associated_with": ("associate", "correlat", "linked", "association"),
    "deficiency_causes": ("deficiency", "deficient", "insufficiency"),
    "has_nutrient": ("contain", "content", "composition", "rich in"),
    "has_contaminant": ("contain", "contamin", "residue", "accumulat", "present in"),
}

_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": sorted(VALID_DECISIONS)},
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
                    "quoted_span": {"type": "string"},
                    "stance": {"type": "string", "enum": ["supports", "contradicts"]},
                },
                "required": ["chunk_id", "quoted_span", "stance"],
                "additionalProperties": False,
            },
        },
        "rationale": {"type": "string"},
    },
    "required": ["decision", "matches", "evidence", "rationale"],
    "additionalProperties": False,
}


def judge_document(client: OllamaClient, candidate: dict[str, Any], document: dict[str, Any]) -> dict[str, Any]:
    system = """Judge this exact scientific claim against chunks from ONE external paper. Use no background knowledge. A related topic, a different intervention/material/population, a reversed direction, or a mere association is not support for a causal claim. Quote one or two complete consecutive evidence sentences for support or contradiction; otherwise say insufficient. Return JSON only."""
    payload = {"candidate_id": candidate["candidate_id"], "candidate": {key: candidate.get(key) for key in ("subject", "relation", "object", "subject_type", "object_type", "certainty", "qualifiers")},
        "evidence_document_id": document["evidence_document_id"], "evidence_document": {key: document.get(key) for key in ("title", "doi", "publication_year")},
        "evidence_chunks": document["evidence_chunks"]}
    user = ("Return ONE complete JSON object with exactly these keys: decision, matches, evidence, rationale. "
            "matches contains boolean subject, object, relation, direction, and certainty fields. For supported, all must be true. "
            "evidence is [] for insufficient. Otherwise it is a one-item list: {chunk_id, quoted_span, stance}; quote one or two complete consecutive sentences (up to 70 words) copied exactly from the chunk. "
            "stance is supports or contradicts. Explain any mismatch briefly; do not paste a full chunk. INPUT: " + __import__("json").dumps(payload, ensure_ascii=False))
    decision = client.chat_json(system, user, max_tokens=620, json_schema=_DECISION_SCHEMA)
    decision.setdefault("candidate_id", candidate["candidate_id"])
    decision.setdefault("evidence_document_id", document["evidence_document_id"])
    decision.setdefault("evidence", [])
    errors = decision_errors({"candidate": candidate, "evidence_documents": [document]}, decision)
    if decision.get("decision") == "supported":
        quotes = " ".join(str(item.get("quoted_span", "")).casefold() for item in decision.get("evidence", []) if isinstance(item, dict))
        if not any(cue in quotes for cue in RELATION_CUES.get(candidate["relation"], ())):
            errors.append("quoted evidence lacks a compatible relation cue; requires review")
        if candidate["relation"] in {"causes", "increases", "reduces", "leads_to", "deficiency_causes"} and re.search(
                r"\b(?:associated with|association between|correlated with|linked to)\b", quotes):
            errors.append("associative evidence cannot prove a directional or causal claim")
    if errors:
        return {"candidate_id": candidate["candidate_id"], "evidence_document_id": document["evidence_document_id"],
            "decision": "needs_review", "evidence": [], "rationale": "Invalid model decision: " + "; ".join(errors),
            "matches": {}}
    return decision


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
            if len(quote.split()) > 70 or len(quote.split()) < 6:
                errors.append("decisive quote must contain 6-70 words")
            expected_stance = "supports" if decision["decision"] == "supported" else "contradicts"
            if item.get("stance") != expected_stance:
                errors.append("evidence stance does not match decision")
            if candidate:
                for field in ("subject", "object"):
                    if not entity_anchored(str(candidate.get(field, "")), quote):
                        errors.append(f"quoted evidence does not explicitly name the claim {field}")
                qualifiers = candidate.get("qualifiers") or {}
                for field in ("crop_or_species", "population"):
                    value = str(qualifiers.get(field, "")).strip()
                    if value and not entity_anchored(value, quote):
                        errors.append(f"quoted evidence does not explicitly match {field}")
    return errors


def _match_value(decision: dict[str, Any], legacy_field: str) -> Any:
    """Read the compact v8 match object while accepting legacy decision files."""
    compact_name = legacy_field.removesuffix("_match")
    matches = decision.get("matches")
    if isinstance(matches, dict) and compact_name in matches:
        return matches[compact_name]
    return decision.get(legacy_field)
