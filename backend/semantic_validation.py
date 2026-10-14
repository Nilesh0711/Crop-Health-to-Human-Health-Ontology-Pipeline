"""One-pass ontology typing and project-scope screening for extracted claims."""

from __future__ import annotations

import json
from typing import Any

from external_validator.llm import OllamaClient
from .ontology_schema import ENTITY_DEFINITIONS, PATHWAY_DESCRIPTION, pathway_error


_SEMANTIC_SCHEMA = {
    "type": "object",
    "properties": {
        "subject_type": {"type": "string", "enum": sorted(ENTITY_DEFINITIONS)},
        "object_type": {"type": "string", "enum": sorted(ENTITY_DEFINITIONS)},
        "in_scope": {"type": "boolean"},
        "ambiguous": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": ["subject_type", "object_type", "in_scope", "ambiguous", "rationale"],
    "additionalProperties": False,
}


def validate_claim_semantics(candidate: dict[str, Any], client: OllamaClient) -> dict[str, Any]:
    """Classify canonical entity types and scope with one model request.

    The source-entailment stage owns relation, direction, certainty, and exact
    evidence alignment. A classifier disagreement alone cannot veto an
    otherwise structurally valid extracted edge: the same source phrase can
    denote a management setting while the canonical endpoint names its crop.
    """
    evidence = str(candidate.get("source_evidence_span") or "")
    packet = str(candidate.get("source_context_packet") or "")
    heading = str(candidate.get("source_evidence_section_heading")
                  or candidate.get("source_section_heading") or "").strip()
    offset = packet.find(evidence)
    nearby = (packet[max(0, offset - 700):offset] + " [EVIDENCE] "
              + packet[offset + len(evidence):offset + len(evidence) + 500]
              if offset >= 0 else packet[:1200])
    if heading:
        nearby = f"[LOCAL SECTION HEADING] {heading}. " + nearby

    system = (
        "Classify the CANONICAL subject and object labels of one extracted scientific edge, "
        "and decide whether that edge belongs to the supplied project pathway. "
        "The source mentions and sentence are context for understanding those labels; do not classify "
        "a longer source mention in place of the canonical label. A crop named inside a farming-system "
        "phrase is still a Crop when the canonical subject is that crop; whether the sentence supports "
        "the proposed relation is decided by a separate source-evidence check. "
        "A named crop, harvested grain, kernel or seed is Crop even when infected. A management "
        "system is AgriculturalPractice. A named toxin is Contaminant. "
        "Scope concerns the soil/crop/food-to-human-health pathway; exclude economics, trade, "
        "infrastructure, generic food security, and yield-only claims. Do not decide relation "
        "faithfulness, direction, certainty, or world truth here. Return JSON only."
    )
    payload = {
        "project_pathway": PATHWAY_DESCRIPTION,
        "entity_types": ENTITY_DEFINITIONS,
        "canonical_claim": {key: candidate.get(key) for key in
                            ("subject", "subject_type", "relation", "object", "object_type")},
        "source_mentions": {key: candidate.get(key) for key in
                            ("subject_source_mention", "object_source_mention")},
        "selected_evidence_span": evidence,
        "nearby_context": nearby,
    }
    judged = client.chat_json(system, json.dumps(payload, ensure_ascii=False),
                              max_tokens=320, json_schema=_SEMANTIC_SCHEMA)
    classified = {"subject_type": judged.get("subject_type"),
                  "object_type": judged.get("object_type")}
    original = {"subject_type": candidate.get("subject_type"),
                "object_type": candidate.get("object_type")}
    original_shape_error = pathway_error(candidate)
    classified_shape_error = pathway_error(candidate, **classified)
    result = {"candidate_id": candidate["candidate_id"],
              "decision": "accepted", "action": "accept",
              "rationale": str(judged.get("rationale") or "").strip()[:700]}

    if judged.get("in_scope") is False:
        result.update(decision="rejected", action="reject",
                      reasons=["Claim is outside the project pathway"])
    elif judged.get("in_scope") is not True:
        result.update(decision="needs_review", action="review",
                      reasons=["Project scope was not confirmed"])
    elif judged.get("ambiguous") is True and original_shape_error:
        result.update(decision="needs_review", action="review",
                      reasons=["Ontology type or project scope is ambiguous"])
    elif original_shape_error:
        if classified_shape_error:
            result.update(decision="rejected", action="reject",
                          reasons=[original_shape_error])
        else:
            result.update(action="normalize_types", normalized_types=classified,
                          type_normalization={"changed": [name for name in classified
                                                          if classified[name] != original[name]]})
    elif classified != original:
        if classified_shape_error:
            # The classifier can mistake the longer source phrase for the
            # canonical entity. Keep the extracted shape and record the vote.
            result["type_disagreement"] = {"extracted": original, "classified": classified,
                                           "classified_shape_error": classified_shape_error}
        elif judged.get("ambiguous") is True:
            result["type_disagreement"] = {"extracted": original, "classified": classified}
        else:
            result.update(action="normalize_types", normalized_types=classified,
                          type_normalization={"changed": [name for name in classified
                                                          if classified[name] != original[name]]})
    return result
