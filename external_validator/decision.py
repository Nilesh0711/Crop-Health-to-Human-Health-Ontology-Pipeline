"""Evidence-bound Qwen entailment decisions and invariant checks."""

from __future__ import annotations

import re
from typing import Any

from .claim_matching import entity_anchored, entity_terms
from .llm import OllamaClient
from .nli import NLIValidator, claim_hypothesis
from .retrieval import normalize_text


VALID_DECISIONS = {"supported", "contradicted", "insufficient", "needs_review"}

# A final deterministic safeguard after Qwen's structured decision.  These are
# lexical stems/phrases rather than semantic synonyms: they prevent a supported
# result from being accepted when its derived source quote contains no relation
# signal, while avoiding needless reviews for ordinary scientific wording such
# as "detected in" or "enhanced".
RELATION_CUES = {
    "affects": ("affect", "impact", "influence", "alter", "change", "modulat", "regulat", "reduce", "decrease", "increase", "loss", "damage"),
    "causes": ("cause", "induc", "trigger", "produce", "lead to", "led to", "result in", "resulted in"),
    "reduces": ("reduce", "decreas", "lower", "suppress", "inhibit", "diminish", "attenuat", "impair", "protect against", "loss"),
    "increases": ("increase", "elevat", "rais", "higher", "enhanc", "promot", "boost", "augment", "bolster"),
    "leads_to": ("lead to", "led to", "result in", "resulted in", "produce", "trigger", "consequence"),
    "associated_with": ("associate", "correlat", "linked", "relationship between", "related to"),
    "deficiency_causes": ("deficiency", "deficient", "insufficiency", "deplet", "cause", "induc", "lead to", "result in"),
    "has_nutrient": ("contain", "content", "composition", "rich in", "source of", "provide", "concentration", "level"),
    "has_contaminant": ("contain", "contamin", "residue", "accumulat", "present in", "detected", "found in", "occur", "concentration", "content"),
    "contains": ("contain", "compris", "include", "content", "concentration", "presence"),
    "has_trait": ("has", "have", "had", "with", "exhibit", "characteri", "show"),
    "higher_than": ("higher", "greater", "more", "exceed"),
    "lower_than": ("lower", "less", "fewer", "below"),
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
                    "sentence_numbers": {
                        "type": "array", "minItems": 1, "maxItems": 2,
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
    "required": ["decision", "matches", "evidence", "rationale"],
    "additionalProperties": False,
}


_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“(\[])")
_GENERIC_ALIAS_TERMS = {
    "approach", "approaches", "method", "methods", "management", "practice", "practices",
    "technique", "techniques", "treatment", "treatments", "system", "systems", "stress",
    "level", "levels", "concentration", "concentrations", "content", "contents",
}


def _sentences(text: object) -> list[str]:
    """Split an evidence chunk into stable source sentences for model selection."""
    compact = " ".join(str(text or "").split())
    return [sentence.strip() for sentence in _SENTENCE_BOUNDARY_RE.split(compact) if sentence.strip()]


def _numbered_chunks(document: dict[str, Any]) -> list[dict[str, Any]]:
    """Expose sentence IDs, rather than free-form quotation copying, to Qwen."""
    numbered: list[dict[str, Any]] = []
    for chunk in document.get("evidence_chunks", []):
        sentences = _sentences(chunk.get("text", ""))
        if not sentences:
            continue
        numbered.append({
            "chunk_id": chunk.get("chunk_id"),
            "sentences": [{"number": index, "text": sentence} for index, sentence in enumerate(sentences, 1)],
        })
    return numbered


def _derive_evidence_quotes(document: dict[str, Any], decision: dict[str, Any]) -> list[str]:
    """Derive exact stored quotations from selected sentence IDs.

    The model never gets to manufacture a quote.  A failed ID selection remains
    a review state, preserving the same evidence-bound safety property without
    inflating review counts due to harmless punctuation or copy errors.
    """
    errors: list[str] = []
    by_id = {item.get("chunk_id"): _sentences(item.get("text", ""))
             for item in document.get("evidence_chunks", [])}
    evidence = decision.get("evidence", [])
    if not isinstance(evidence, list):
        return ["evidence must be a list"]
    for item in evidence:
        if not isinstance(item, dict):
            errors.append("each evidence item must be an object")
            continue
        sentences = by_id.get(item.get("chunk_id"))
        numbers = item.get("sentence_numbers")
        if sentences is None:
            errors.append("evidence chunk_id is not in the cited document")
            continue
        if not isinstance(numbers, list) or not 1 <= len(numbers) <= 2 or any(
                isinstance(number, bool) or not isinstance(number, int) for number in numbers):
            errors.append("evidence must select one or two sentence numbers")
            continue
        chosen = sorted(set(numbers))
        if len(chosen) != len(numbers) or chosen != list(range(chosen[0], chosen[0] + len(chosen))):
            errors.append("selected evidence sentence numbers must be consecutive")
            continue
        if chosen[0] < 1 or chosen[-1] > len(sentences):
            errors.append("selected evidence sentence number is outside the chunk")
            continue
        item["quoted_span"] = " ".join(sentences[number - 1] for number in chosen)
    return errors


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
    candidate_terms = entity_terms(str(candidate_entity or "")) - _GENERIC_ALIAS_TERMS
    evidence_terms = entity_terms(str(evidence_mention or "")) - _GENERIC_ALIAS_TERMS
    if not candidate_terms or not evidence_terms:
        return False
    return candidate_terms <= evidence_terms or evidence_terms <= candidate_terms


def _entity_evidence_compatible(candidate_entity: object, evidence_mention: object, quote: object) -> bool:
    if not _mention_in_quote(evidence_mention, quote):
        return False
    return entity_anchored(str(candidate_entity or ""), str(quote or "")) or _alias_compatible(candidate_entity, evidence_mention)


def _quote_has_relation_cue(relation: object, quote: object) -> bool:
    """Apply the final lexical relation safeguard to a stored source quote."""
    text = str(quote or "").casefold()
    relation_name = str(relation or "")
    if relation_name == "deficiency_causes":
        # Deficiency alone is merely a condition.  It must be coupled with an
        # explicit causal/downstream expression before this special relation
        # can enter the main graph.
        deficiency_cues = ("deficien", "insufficiency", "deplet")
        causal_cues = ("cause", "induc", "lead to", "led to", "result in", "resulted in")
        return any(cue in text for cue in deficiency_cues) and any(cue in text for cue in causal_cues)
    return any(cue in text for cue in RELATION_CUES.get(relation_name, ()))


def judge_document(client: OllamaClient, candidate: dict[str, Any], document: dict[str, Any],
                   nli_validator: NLIValidator | None = None) -> dict[str, Any]:
    system = """Judge this exact scientific claim against numbered sentences from ONE external paper. Use no background knowledge. A related topic, a different intervention/material/population/comparator, a reversed direction, or a mere association is not support for a directional or causal claim. This is evidence alignment: an external paper that explicitly reports X increases/reduces Y can support that directional edge without independently proving a universal causal mechanism, but its modality must be at least as strong as the candidate wording. For support or contradiction, select one or two consecutive sentence numbers and copy the exact subject mention, object mention, and relation trigger from them; otherwise say insufficient. Return JSON only."""
    payload = {"candidate_id": candidate["candidate_id"], "candidate": {key: candidate.get(key) for key in ("subject", "relation", "object", "subject_type", "object_type", "certainty", "qualifiers")},
        "evidence_document_id": document["evidence_document_id"], "evidence_document": {key: document.get(key) for key in ("title", "doi", "publication_year")},
        "evidence_chunks": _numbered_chunks(document)}
    user = ("Return ONE complete JSON object with exactly these keys: decision, matches, evidence, rationale. "
            "matches contains boolean subject, object, relation, direction, and certainty fields. For supported, all must be true. "
            "evidence is [] for insufficient. Otherwise it is a one-item list: {chunk_id, sentence_numbers, subject_mention, "
            "object_mention, relation_trigger, stance}; select one or two complete consecutive numbered sentences (up to 70 words total). "
            "stance is supports or contradicts. Explain any mismatch briefly; do not paste a full chunk. INPUT: " + __import__("json").dumps(payload, ensure_ascii=False))
    decision = client.chat_json(system, user, max_tokens=620, json_schema=_DECISION_SCHEMA)
    decision.setdefault("candidate_id", candidate["candidate_id"])
    decision.setdefault("evidence_document_id", document["evidence_document_id"])
    decision.setdefault("evidence", [])
    quote_errors = _derive_evidence_quotes(document, decision)
    errors = [*quote_errors, *decision_errors({"candidate": candidate, "evidence_documents": [document]}, decision)]
    if decision.get("decision") == "supported":
        quotes = " ".join(str(item.get("quoted_span", "")).casefold() for item in decision.get("evidence", []) if isinstance(item, dict))
        if not _quote_has_relation_cue(candidate["relation"], quotes):
            errors.append("quoted evidence lacks a compatible relation cue; requires review")
        if candidate["relation"] in {"causes", "increases", "reduces", "leads_to", "deficiency_causes"} and re.search(
                r"\b(?:associated with|association between|correlated with|linked to)\b", quotes):
            errors.append("associative evidence cannot prove a directional or causal claim")
    if errors:
        return {"candidate_id": candidate["candidate_id"], "evidence_document_id": document["evidence_document_id"],
            "decision": "needs_review", "evidence": [], "rationale": "Invalid model decision: " + "; ".join(errors),
            "matches": {}, "evidence_level": document.get("evidence_level", "independent_external"),
            "retrieval_route": document.get("retrieval_route", "broad_search")}
    decision["evidence_level"] = document.get("evidence_level", "independent_external")
    decision["retrieval_route"] = document.get("retrieval_route", "broad_search")
    decision["independence_group"] = document.get("independence_group")
    if nli_validator is not None and decision.get("decision") in {"supported", "contradicted"}:
        quote = " ".join(str(item.get("quoted_span") or "") for item in decision.get("evidence", []))
        try:
            nli_scores = nli_validator.score(quote, claim_hypothesis(candidate))
            decision["nli"] = {"model": nli_validator.settings.nli_model, "scores": nli_scores}
            # NLI is only a conservative veto. It can never promote an
            # insufficient Qwen/anchor result into support.
            if decision["decision"] == "supported" and nli_scores.get("contradiction", 0.0) >= 0.75:
                decision["decision"] = "needs_review"
                decision["rationale"] = (str(decision.get("rationale") or "")
                                         + " Independent NLI strongly contradicts the proposed claim.").strip()
        except Exception as exc:
            decision["nli"] = {"model": nli_validator.settings.nli_model, "error": str(exc)[:240]}
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
            trigger = item.get("relation_trigger")
            if trigger is not None and not _mention_in_quote(trigger, quote):
                errors.append("quoted evidence does not contain the stated relation trigger")
            if candidate:
                for field in ("subject", "object"):
                    evidence_mention = item.get(f"{field}_mention")
                    matches_entity = (
                        _entity_evidence_compatible(candidate.get(field, ""), evidence_mention, quote)
                        if evidence_mention is not None
                        else entity_anchored(str(candidate.get(field, "")), quote)
                    )
                    if not matches_entity:
                        errors.append(f"quoted evidence does not explicitly name the claim {field}")
                qualifiers = candidate.get("qualifiers") or {}
                for field in ("crop_or_species", "population", "comparator", "metric"):
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
