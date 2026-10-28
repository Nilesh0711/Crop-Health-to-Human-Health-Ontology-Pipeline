"""Document-bound evidence checks for joint entailment decisions."""

from __future__ import annotations

import re
from copy import deepcopy
from typing import Any

from backend.domain.claim_matching import entity_anchored, entity_terms
from backend.domain.alignment import (
    ALIGNMENT_SCHEMA,
    ENTITY_ALIGNMENTS_SCHEMA,
    grounded_alignment,
)
from backend.providers.nli import NLIValidator, claim_hypothesis
from backend.stages.literature_retrieval.clients import normalize_text, scholarly_sentences


VALID_DECISIONS = {"supported", "contradicted", "insufficient", "needs_review"}
MAX_EVIDENCE_WORDS = 180

# A final deterministic safeguard after the evidence model's structured decision.  These are
# lexical stems/phrases rather than semantic synonyms: they prevent a supported
# result from being accepted when its derived source quote contains no relation
# signal, while avoiding needless reviews for ordinary scientific wording such
# as "detected in" or "enhanced".
RELATION_CUES = {
    "affects": ("affect", "impact", "influence", "alter", "change", "modulat", "regulat", "determin", "drive", "contribut", "reduce", "decrease", "increase", "enhanc", "promot", "improv", "suppress", "inhibit", "loss", "damage"),
    "causes": ("cause", "induc", "trigger", "produce", "lead to", "led to", "result in", "resulted in"),
    "reduces": ("reduce", "decreas", "lower", "suppress", "inhibit", "diminish", "attenuat", "impair", "mitigat", "alleviat", "protect against", "loss"),
    "increases": ("increase", "elevat", "rais", "higher", "enhanc", "promot", "improv", "foster", "stimulat", "facilitat", "boost", "augment", "bolster"),
    "leads_to": ("lead to", "led to", "result in", "resulted in", "produce", "trigger", "consequence"),
    "associated_with": ("associate", "correlat", "linked", "relationship between", "related to"),
    "deficiency_causes": ("deficiency", "deficient", "insufficiency", "deplet", "cause", "induc", "lead to", "result in"),
    "has_nutrient": ("contain", "content", "composition", "rich in", "source of", "provide", "concentration", "level"),
    # A contaminant can be stated either as material composition ("detected
    # in grain") or as production/formation within that material.  The latter
    # is common for fungal toxins, so it is an explicit lexical cue rather
    # than a broad semantic relaxation.
    "has_contaminant": ("contain", "contamin", "residue", "accumulat", "present in", "detected", "found in", "occur", "concentration", "content", "produc", "increase", "elevat"),
    "contains": ("contain", "compris", "include", "content", "concentration", "presence"),
    "has_trait": ("has", "have", "had", "with", "exhibit", "characteri", "show"),
    "higher_than": ("higher", "greater", "more", "exceed"),
    "lower_than": ("lower", "less", "fewer", "below"),
}

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


_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“(\[])")
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


def _sentences(text: object) -> list[str]:
    """Split an evidence chunk into stable source sentences for model selection."""
    return scholarly_sentences(str(text or ""))


def _numbered_chunks(document: dict[str, Any], candidate: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Expose sentence IDs, focusing exact claim-bearing sentences when present.

    The focus step does not decide entailment.  It only prevents a small model
    from citing a later, topically related sentence when the same retrieved
    chunk contains a sentence that explicitly names both claim endpoints and a
    compatible relation cue.  When no such lexical sentence exists, all
    sentences remain available so semantic paraphrases can still be judged.
    """
    numbered: list[dict[str, Any]] = []
    for chunk in document.get("evidence_chunks", []):
        sentences = _sentences(chunk.get("text", ""))
        if not sentences:
            continue
        sentence_rows = [{"number": index, "text": sentence}
                         for index, sentence in enumerate(sentences, 1)]
        if candidate:
            subject_terms = _concept_terms(candidate.get("subject"))
            object_terms = _concept_terms(candidate.get("object"))
            exact_rows = []
            for row in sentence_rows:
                sentence_terms = _concept_terms(row["text"])
                subject_named = bool(subject_terms) and subject_terms <= sentence_terms
                object_named = bool(object_terms) and object_terms <= sentence_terms
                relation_named = _quote_has_relation_cue(candidate.get("relation"), row["text"])
                if subject_named and object_named and relation_named:
                    exact_rows.append(row)
            # Keep surrounding sentences available: isolated lexical hits can
            # be methods statements or omit the antecedent of a result.
            priorities = [row["number"] for row in exact_rows[:4]]
        else:
            priorities = []
        numbered.append({
            "chunk_id": chunk.get("chunk_id"),
            **{key: chunk[key] for key in ('source_kind', 'table_id', 'row_number', 'figure_id', 'layout_verified') if key in chunk},
            "sentences": sentence_rows,
            "priority_sentence_numbers": priorities,
            "section_heading": chunk.get("section_heading", ""),
            'paragraph_references': chunk.get('paragraph_references', []),
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
        if not isinstance(numbers, list) or not 1 <= len(numbers) <= 3 or any(
                isinstance(number, bool) or not isinstance(number, int) for number in numbers):
            errors.append("evidence must select one to three sentence numbers")
            continue
        chosen = sorted(set(numbers))
        if len(chosen) != len(numbers):
            errors.append("selected evidence sentence numbers must be consecutive")
            continue
        if chosen[0] < 1 or chosen[-1] > len(sentences):
            errors.append("selected evidence sentence number is outside the chunk")
            continue
        cover = list(range(chosen[0], chosen[-1] + 1))
        if chosen != cover:
            if len(cover) > 3:
                errors.append("selected evidence sentence numbers must be consecutive")
                continue
            # The stored proof is the complete bounded interval, never a splice
            # hiding intervening text. All normal quote/alignment/NLI checks run
            # on that actual interval. Preserve the model's selection for audit.
            item['selection_adjustment'] = {'original_sentence_numbers': list(numbers),
                                            'basis': 'bounded_consecutive_cover'}
            chosen = cover
            item['sentence_numbers'] = cover
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


def _quote_has_relation_cue(relation: object, quote: object) -> bool:
    """Lexical attention aid for sentence ranking and nonblocking review notes."""
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


def _relation_review_hints(candidate: dict[str, Any], decision: dict[str, Any]) -> list[str]:
    """Lexical concerns for inspection, never a second semantic verdict."""
    errors: list[str] = []
    for item in decision.get("evidence", []):
        quote, trigger = str(item.get("quoted_span") or ""), str(item.get("relation_trigger") or "")
        relation = candidate.get("relation")
        # Models judge predicate meaning and anchor exact triggers. A small
        # cue dictionary cannot cover the open scientific relation vocabulary.
        if (not decision.get("entity_alignment") or relation == "deficiency_causes") and not _quote_has_relation_cue(relation, quote):
            errors.append("review_unrecognized_relation_wording")
        if relation in {"causes", "increases", "reduces", "leads_to", "deficiency_causes"} and re.search(
                r"\b(?:associated with|association between|correlated with|linked to)\b", trigger, re.IGNORECASE):
            errors.append("review_association_and_direction")
        increasing = bool(re.search(r"\b(?:increas|enhanc|elevat|promot|higher)\w*\b", trigger, re.IGNORECASE))
        decreasing = bool(re.search(r"\b(?:decreas|reduc|suppress|inhibit|lower)\w*\b", trigger, re.IGNORECASE))
        if (relation == "increases" and decreasing and not increasing
                or relation == "reduces" and increasing and not decreasing):
            errors.append("review_relation_direction")
    return errors


def guard_document_decision(candidate: dict[str, Any], document: dict[str, Any],
                            decision: dict[str, Any],
                            nli_validator: NLIValidator | None = None) -> dict[str, Any]:
    """Validate one paper's portion of the joint response without another call."""
    decision = deepcopy(decision)
    decision.setdefault("candidate_id", candidate["candidate_id"])
    decision.setdefault("evidence_document_id", document["evidence_document_id"])
    decision.setdefault("evidence", [])
    quote_errors = _derive_evidence_quotes(document, decision)
    packet = {"candidate": candidate, "evidence_documents": [document]}
    errors = [*quote_errors, *decision_errors(packet, decision)]
    relation_hints = _relation_review_hints(candidate, decision)
    if relation_hints:
        decision['review_hints'] = relation_hints
    proposed = deepcopy(decision)
    if errors:
        decision.update(decision="needs_review", evidence=[], matches={},
                        rationale="Invalid model decision: " + "; ".join(errors),
                        rejected_model_decision=proposed, guardrail_reasons=errors)
    decision["evidence_level"] = document.get("evidence_level", "independent_external")
    decision["retrieval_route"] = document.get("retrieval_route", "broad_search")
    decision["independence_group"] = document.get("independence_group")
    if nli_validator is not None:
        quote = " ".join(str(item.get("quoted_span") or "") for item in decision.get("evidence", []))
        try:
            if quote:
                nli_scores = nli_validator.score(quote, claim_hypothesis(candidate))
                decision["nli"] = {"model": nli_validator.settings.nli_model, "scores": nli_scores}
                if (decision["decision"] in {"needs_review", "insufficient"}
                        and getattr(nli_validator.settings, "enable_nli_promotion", False)
                        and nli_scores.get("entailment", 0.0) >= 0.82):
                    promoted = deepcopy(decision)
                    promoted["decision"] = "supported"
                    promotion_errors = decision_errors(packet, promoted)
                    if not promotion_errors:
                        decision["decision"] = "supported"
                        decision["nli"]["promotion_applied"] = True
                        decision["rationale"] = str(decision.get("rationale") or "") + " NLI-assisted promotion passed the full support evidence contract."
                    else:
                        decision["nli"]["promotion_blocked"] = promotion_errors
                elif decision["decision"] == "supported" and nli_scores.get("contradiction", 0.0) >= 0.75:
                    decision["decision"] = "needs_review"
                    decision["rationale"] = (str(decision.get("rationale") or "")
                                             + " Independent NLI strongly contradicts the proposed claim.").strip()
            else:
                decision["nli"] = {"model": nli_validator.settings.nli_model,
                                   "skipped": "No admissible selected evidence span; scores cannot create evidence"}
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
