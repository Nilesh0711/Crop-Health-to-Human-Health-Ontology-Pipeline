"""Qwen semantic and project-scope gate for extracted claims."""

from __future__ import annotations

import json
from typing import Any

from external_validator.llm import OllamaClient
from .ontology_schema import ENTITY_DEFINITIONS, PATHWAY_DESCRIPTION, RELATION_DEFINITIONS


_ENTITY_LABELS = sorted(ENTITY_DEFINITIONS)
_RELATION_LABELS = sorted(RELATION_DEFINITIONS)

_TYPE_SCHEMA = {
    "type": "object",
    "properties": {
        "subject_type": {"type": "string", "enum": _ENTITY_LABELS},
        "object_type": {"type": "string", "enum": _ENTITY_LABELS},
        "ambiguous": {"type": "boolean"},
        "rationale": {"type": "string"},
    },
    "required": ["subject_type", "object_type", "ambiguous", "rationale"],
    "additionalProperties": False,
}

_RELATION_SCHEMA = {
    "type": "object",
    "properties": {
        "in_scope": {"type": "boolean"},
        "relation_match": {"type": "boolean"},
        "direction_match": {"type": "boolean"},
        "atomic_claim": {"type": "boolean"},
        "ambiguous": {"type": "boolean"},
        "suggested_relation": {"type": "string", "enum": _RELATION_LABELS},
        "rationale": {"type": "string"},
    },
    "required": ["in_scope", "relation_match", "direction_match", "atomic_claim", "ambiguous",
                 "suggested_relation", "rationale"],
    "additionalProperties": False,
}


def validate_claim_semantics(candidate: dict[str, Any], client: OllamaClient) -> dict[str, Any]:
    """Classify entity types first, then independently check scope/relation semantics."""
    evidence = str(candidate.get("source_evidence_span", ""))
    type_system = (
        "Classify the two named entities in an extracted scientific claim using only the supplied ontology labels "
        "and source evidence. Classify what each entity phrase denotes, not the broader topic around it. "
        "A consumption or dietary-exposure phrase is HumanExposure, not AgriculturalPractice. Crop composition, "
        "quality, concentration pattern, or resistance level is PlantTrait; a named chemical or nutritional component "
        "is Nutrient or Contaminant. A disease-resistance mechanism is PlantProcess, while the named disease itself is "
        "PlantDisease. Human biomarkers, immune responses, metabolism, and epigenetic changes are "
        "HumanBiologicalProcess. Set ambiguous true only when either entity genuinely permits multiple types. Return JSON only."
    )
    type_payload = {
        "entity_types": ENTITY_DEFINITIONS,
        "subject": candidate.get("subject"),
        "object": candidate.get("object"),
        "source_evidence_span": evidence,
    }
    typed = client.chat_json(type_system, json.dumps(type_payload, ensure_ascii=False),
                             max_tokens=300, json_schema=_TYPE_SCHEMA)

    relation_system = (
        "Check the project relevance and relation semantics of one extracted scientific edge. Do not judge whether "
        "the finding is scientifically true and do not use world knowledge. An edge is in scope when it can be one "
        "segment of the soil-to-human-health pathway; it does not need to contain the entire pathway or directly reach "
        "human health. For example, fertilizer affects soil chemistry, soil biota affects nutrient uptake, a crop has "
        "a nutrient or contaminant, and human exposure affects a biomarker are all potentially in scope. Exclude only "
        "economics, infrastructure, trade, generic food security, yield-only outcomes, or unrelated topics. Check that "
        "the relation preserves the evidence wording and direction and that the claim is one atomic edge. Association "
        "never proves causes, increases, reduces, or leads_to. Return JSON only."
    )
    relation_payload = {
        "project_pathway": PATHWAY_DESCRIPTION,
        "relations": RELATION_DEFINITIONS,
        "claim": {key: candidate.get(key) for key in
                  ("subject", "relation", "object", "certainty", "qualifiers")},
        "classified_subject_type": typed.get("subject_type"),
        "classified_object_type": typed.get("object_type"),
        "source_evidence_span": evidence,
    }
    judged = client.chat_json(relation_system, json.dumps(relation_payload, ensure_ascii=False),
                              max_tokens=360, json_schema=_RELATION_SCHEMA)

    subject_type_match = typed.get("subject_type") == candidate.get("subject_type")
    object_type_match = typed.get("object_type") == candidate.get("object_type")
    relation_match = (judged.get("relation_match") is True
                      and judged.get("suggested_relation") == candidate.get("relation"))
    checks = {
        "in_scope": judged.get("in_scope"),
        "subject_type_match": subject_type_match,
        "object_type_match": object_type_match,
        "relation_match": relation_match,
        "direction_match": judged.get("direction_match"),
        "atomic_claim": judged.get("atomic_claim"),
    }
    ambiguous = typed.get("ambiguous") is True or judged.get("ambiguous") is True
    failed = [name for name, passed in checks.items() if passed is not True]
    decision = "needs_review" if ambiguous else "rejected" if failed else "accepted"
    reasons = [f"{name} was not confirmed" for name in failed]
    if ambiguous:
        reasons.append("Qwen marked the type or relation as ambiguous")

    rationales = [str(typed.get("rationale", "")).strip(), str(judged.get("rationale", "")).strip()]
    result = {
        "candidate_id": candidate["candidate_id"],
        "decision": decision,
        "rationale": " ".join(value for value in rationales if value)[:700],
    }
    if reasons:
        result["reasons"] = reasons
    suggested = {
        "subject_type": typed.get("subject_type"),
        "object_type": typed.get("object_type"),
        "relation": judged.get("suggested_relation"),
    }
    if any(suggested[key] != candidate.get(key) for key in ("subject_type", "object_type", "relation")):
        result["suggested"] = suggested
    return result
