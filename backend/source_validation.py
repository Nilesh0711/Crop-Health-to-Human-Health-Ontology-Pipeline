"""Check whether the originating PDF's cited text entails an extracted claim."""

from __future__ import annotations

import json
import re
from typing import Any

from external_validator.claim_matching import entity_anchored
from external_validator.llm import OllamaClient
_CAUSAL_RELATIONS = {"causes", "increases", "reduces", "leads_to", "deficiency_causes"}
_ASSOCIATION_ONLY = re.compile(r"\b(?:associated with|association between|correlated with|linked to)\b", re.IGNORECASE)
_DIRECTION_CUES = {
    "causes": ("cause", "induc", "result", "lead", "trigger", "produc"),
    "increases": ("increas", "higher", "elevat", "enhanc", "rais", "promot"),
    "reduces": ("reduc", "decreas", "lower", "suppress", "inhibit", "declin"),
    "leads_to": ("lead", "result", "caus", "contribut"),
    "deficiency_causes": ("deficien", "insufficien", "deplet"),
}

_SOURCE_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["entailed", "unsupported", "needs_review"]},
        "relation_match": {"type": "boolean"},
        "direction_match": {"type": "boolean"},
        "certainty_match": {"type": "boolean"},
        "proof": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "relation": {"type": "string"},
                "object": {"type": "string"},
            },
            "required": ["subject", "relation", "object"],
            "additionalProperties": False,
        },
        "rationale": {"type": "string"},
    },
    "required": ["decision", "relation_match", "direction_match", "certainty_match", "proof", "rationale"],
    "additionalProperties": False,
}


def _nearby_context(candidate: dict[str, Any], span: str) -> str:
    packet = str(candidate.get("source_context_packet", ""))
    offset = packet.find(span)
    if offset < 0:
        return packet[:1200]
    return packet[max(0, offset - 600):offset] + " [EVIDENCE SPAN] " + packet[offset + len(span):offset + len(span) + 350]


def validate_source_claim(candidate: dict[str, Any], client: OllamaClient) -> dict[str, Any]:
    span = str(candidate.get("source_evidence_span", "")).strip()
    numbers = candidate.get("source_evidence_sentence_numbers", [])
    base = {"candidate_id": candidate["candidate_id"]}
    if not span or not isinstance(numbers, list) or not 1 <= len(numbers) <= 3:
        return {**base, "decision": "unsupported", "rationale": "Missing source evidence."}

    system = (
        "Act as a skeptical source-evidence verifier. The extracted claim is an untrusted hypothesis that may be wrong. "
        "Judge whether the selected SOURCE PDF evidence span directly entails every part of that exact claim. "
        "Use the nearby context only to resolve an explicit abbreviation or pronoun; never use the paper title or world knowledge as proof. "
        "Check the exact subject, object, relation, direction, and certainty. "
        "Association does not prove causes, increases or reduces. A claim about a different crop, population or material is unsupported. "
        "Return JSON with decision (entailed, unsupported, needs_review), relation_match, direction_match, "
        "certainty_match, proof {subject, relation, object} containing exact substrings of the selected evidence span, "
        "and a brief rationale. Choose needs_review when wording is ambiguous."
    )
    payload = {"claim_hypothesis": {key: candidate.get(key) for key in ("subject", "relation", "object", "certainty", "qualifiers")},
               "selected_evidence_span": span, "nearby_context": _nearby_context(candidate, span)}
    judged = client.chat_json(system, json.dumps(payload, ensure_ascii=False), max_tokens=520,
                              json_schema=_SOURCE_DECISION_SCHEMA)
    decision = str(judged.get("decision", "needs_review")).strip().lower()
    reasons: list[str] = []
    if decision not in {"entailed", "unsupported", "needs_review"}:
        decision = "needs_review"
        reasons.append("Invalid source decision")
    if decision == "entailed":
        for field in ("relation_match", "direction_match", "certainty_match"):
            if judged.get(field) is not True:
                reasons.append(f"{field} was not confirmed")
        proof = judged.get("proof") or {
            "subject": judged.get("subject_quote"),
            "relation": judged.get("relation_quote"),
            "object": judged.get("object_quote"),
        }
        for field in ("subject", "relation", "object"):
            quoted = str(proof.get(field, "")).strip()
            if not quoted or quoted.casefold() not in span.casefold():
                reasons.append(f"{field} proof is missing from the selected evidence")
        if not entity_anchored(str(candidate.get("subject", "")), span):
            reasons.append("Subject is not explicitly named in the selected evidence")
        if not entity_anchored(str(candidate.get("object", "")), span):
            reasons.append("Object is not explicitly named in the selected evidence")
        if candidate.get("relation") in _CAUSAL_RELATIONS and _ASSOCIATION_ONLY.search(span):
            reasons.append("Associative evidence does not establish a causal or directional effect")
        cues = _DIRECTION_CUES.get(str(candidate.get("relation", "")))
        if cues and not any(cue in span.casefold() for cue in cues):
            reasons.append("Selected evidence lacks an explicit directional relation cue")
        if reasons:
            decision = "needs_review"
    result = {**base, "decision": decision, "rationale": str(judged.get("rationale", "")).strip()[:400]}
    if reasons:
        result["reasons"] = reasons
    proof = judged.get("proof") or {
        "subject": judged.get("subject_quote"),
        "relation": judged.get("relation_quote"),
        "object": judged.get("object_quote"),
    }
    if any(proof.values()):
        result["proof"] = proof
    return result
