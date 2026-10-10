"""Qwen semantic and project-scope gate for extracted claims."""

from __future__ import annotations

import json
from typing import Any

from external_validator.llm import OllamaClient
from .ontology_schema import (ENTITY_DEFINITIONS, PATHWAY_DESCRIPTION,
                              RELATION_DEFINITIONS, pathway_error)


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
        "qualifiers_complete": {"type": "boolean"},
        "ambiguous": {"type": "boolean"},
        "suggested_relation": {"type": "string", "enum": _RELATION_LABELS},
        "rationale": {"type": "string"},
    },
    "required": ["in_scope", "relation_match", "direction_match", "atomic_claim", "qualifiers_complete", "ambiguous",
                 "suggested_relation", "rationale"],
    "additionalProperties": False,
}


def validate_claim_semantics(candidate: dict[str, Any], client: OllamaClient) -> dict[str, Any]:
    """Validate a candidate and safely apply an unambiguous type-only correction.

    The function never mutates ``candidate``.  For a safe correction it returns
    ``normalized_types``; the pipeline applies it while preserving the original
    extraction as provenance.  We deliberately do not normalize a relation,
    direction, scope judgement, or non-atomic claim: those change the fact being
    asserted and must instead be rejected or sent to review.

    ``decision`` remains compatible with the rest of the pipeline
    (``accepted``, ``rejected``, or ``needs_review``).  ``action`` makes the
    semantic outcome explicit:

    * ``accept``: the extracted labels and claim semantics were confirmed;
    * ``normalize_types``: only one or both entity types were corrected;
    * ``reject``: a clear scope/relation/direction/atomicity failure occurred;
    * ``review``: Qwen marked the classification ambiguous.
    """
    evidence = str(candidate.get("source_evidence_span", ""))
    type_system = (
        "Classify the two named entities in an extracted scientific claim using only the supplied ontology labels "
        "and source evidence. Classify what each entity phrase denotes, not the broader topic around it. "
        "A consumption or dietary-exposure phrase is HumanExposure, not AgriculturalPractice. Crop composition, "
        "quality, concentration pattern, or resistance level is PlantTrait; a named chemical or nutritional component "
        "is Nutrient or Contaminant. Soil itself, a soil fraction, or amendment is SoilMaterial; soil pH, chemical "
        "availability, or concentration is SoilChemicalProperty; an edible processed item is FoodProduct. "
        "A disease-resistance mechanism is PlantProcess, while the named disease itself is "
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
        "the finding is scientifically true and do not use world knowledge. An edge is in scope only when it can be one "
        "segment of the soil-to-human-health pathway: agriculture/climate -> soil -> crop/process/disease/pest/trait -> "
        "nutrient or contaminant -> human exposure -> human biology or health. A directly evidenced edge may skip a stage, "
        "but programme characteristics, economics, infrastructure, trade, generic food security, yield-only outcomes, "
        "and generic sustainability are out of scope. Check that the relation preserves the evidence wording and direction "
        "and that the claim is one atomic edge. Association never proves causes, increases, reduces, or leads_to. "
        "Set qualifiers_complete false if the evidence makes the statement dependent on a crop/species, comparator, metric, "
        "dose, condition, population, or exposure route that is missing from claim qualifiers. Return JSON only."
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

    # Snapshot extracted values before considering a possible normalization.
    # The pipeline retains them as provenance when it applies normalized_types.
    original_types = {
        "subject_type": candidate.get("subject_type"),
        "object_type": candidate.get("object_type"),
    }
    classified_types = {
        "subject_type": typed.get("subject_type"),
        "object_type": typed.get("object_type"),
    }
    subject_type_match = classified_types["subject_type"] == original_types["subject_type"]
    object_type_match = classified_types["object_type"] == original_types["object_type"]
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
    # Only type differences may be repaired.  A mismatch in scope, relation,
    # direction, or atomicity changes the asserted scientific statement, so it
    # must stay rejected/review rather than being silently rewritten.
    claim_semantic_failures = [
        name for name in ("in_scope", "relation_match", "direction_match", "atomic_claim")
        if checks[name] is not True
    ]
    type_changes = [
        name for name in ("subject_type", "object_type")
        if classified_types[name] != original_types[name]
    ]

    # Apply the type-pair / relation-shape gate to Qwen's constrained type
    # labels, not the extractor's often noisy first guess.  This makes the
    # result deterministic after classification while avoiding a brittle
    # word-list ontology.
    pathway_shape_error = pathway_error(candidate, **classified_types)
    if pathway_shape_error:
        claim_semantic_failures.append("pathway_shape")
    if judged.get("qualifiers_complete") is not True:
        claim_semantic_failures.append("qualifiers_complete")

    if ambiguous:
        decision = "needs_review"
        action = "review"
    elif claim_semantic_failures:
        decision = "rejected"
        action = "reject"
    elif type_changes:
        # The only accepted rewrite: both values come from the constrained
        # ontology enum returned by the type classifier, and all fact-level
        # checks have already passed.  The caller owns applying this result so
        # the original extraction can be retained as provenance.
        decision = "accepted"
        action = "normalize_types"
    else:
        decision = "accepted"
        action = "accept"

    # A normalized type mismatch is a recorded correction, not a failure.  Keep
    # ``reasons`` for rejected/review outcomes so status summaries do not count
    # a successfully repaired claim as an error.
    reasons = [] if action == "normalize_types" else [
        f"{name} was not confirmed" for name in failed
    ]
    if pathway_shape_error:
        reasons.append(pathway_shape_error)
    if judged.get("qualifiers_complete") is not True:
        reasons.append("Evidence-dependent qualifiers are missing or incomplete")
    if ambiguous:
        reasons.append("Qwen marked the type or relation as ambiguous")

    rationales = [str(typed.get("rationale", "")).strip(), str(judged.get("rationale", "")).strip()]
    result = {
        "candidate_id": candidate["candidate_id"],
        "decision": decision,
        "action": action,
        "rationale": " ".join(value for value in rationales if value)[:700],
    }
    if reasons:
        result["reasons"] = reasons
    suggested = {
        "subject_type": classified_types["subject_type"],
        "object_type": classified_types["object_type"],
        "relation": judged.get("suggested_relation"),
    }
    if (suggested["subject_type"] != original_types["subject_type"]
            or suggested["object_type"] != original_types["object_type"]
            or suggested["relation"] != candidate.get("relation")):
        result["suggested"] = suggested
    if action == "normalize_types":
        result["normalized_types"] = classified_types
        result["type_normalization"] = {"changed": type_changes}
    return result
