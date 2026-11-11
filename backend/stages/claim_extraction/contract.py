"""Extraction response schema, limits and conservative lexical artifact guards."""

from __future__ import annotations

import re


GENERIC_ENTITIES = {"agriculture", "food", "temperature", "yield", "disease", "soil", "pests", "crop", "health", "nutrition", "nutrient",
                    "crop growth", "crop productivity", "soil health", "soil quality", "nutrient levels",
                    "plantdisease", "soilcondition", "climatefactor", "humanexposure", "humanhealthoutcome", "agriculturalpractice"}


EXTRACTION_MAX_CLAIMS_PER_PACKET = 0  # No application claim-count cap by default.
EXTRACTION_CONTRACT_VERSION = 11


def valid_relation_wording(value: object) -> bool:
    """Require written predicate wording, not a one-character output artifact.

    This is a format check, not a relation whitelist or scientific verdict.
    """
    return isinstance(value, str) and len(value.strip()) >= 2


EXTRACTION_OUTPUT_TOKENS = 3000


COMPARATIVE_CUE_RE = re.compile(r"\b(?:compared with|compared to|versus|vs\.?|than|relative to)\b", re.IGNORECASE)


PURPOSE_ONLY_RE = re.compile(
    r"^\s*(?:use|use of|application|adoption|deployment)\b.+?\b(?:to|for)\s+"
    r"(?:increase|reduce|control|improve|maintain|avoid|prevent)\b", re.IGNORECASE)


FIGURE_OR_TABLE_RE = re.compile(r"^\s*(?:figure|table)\s*\d", re.IGNORECASE)


NON_EMPIRICAL_RE = re.compile(r"\b(?:no information on|no evidence (?:was )?(?:available|exists)|future research)\b", re.IGNORECASE)


ASSOCIATIVE_TRIGGER_RE = re.compile(
    r"\b(?:associated with|association between|correlated with|linked to|relationship between)\b",
    re.IGNORECASE,
)


EXTRACTION_RELATION_CUE_RE = re.compile(
    r"\b(?:affect\w*|influenc\w*|impact\w*|reduc\w*|decreas\w*|lower\w*|suppress\w*|"
    r"inhibit\w*|increas\w*|enhanc\w*|promot\w*|improv\w*|accumulat\w*|contain\w*|produc\w*|detect\w*|found|"
    r"contaminat\w*|associat\w*|correlat\w*|link(?:ed|s)?|higher|greater)\b",
    re.IGNORECASE,
)


EXTRACTION_PATHWAY_TERM_RE = re.compile(
    r"\b(?:soil|crop|plant|root|microb\w*|fung\w*|nutrient\w*|mineral\w*|contamin\w*|"
    r"pesticid\w*|toxin\w*|mycotoxin\w*|metal\w*|pathogen\w*|disease|exposure|intake|human|"
    r"health|phytochemical\w*|antioxidant\w*)\b",
    re.IGNORECASE,
)


CORE_PATHWAY_HINT_RE = re.compile(
    r'\b(?:nutrient\w*|nutrit\w*|contamin\w*|mycotoxin\w*|toxin\w*|exposure|intake|ingestion|'
    r'bioavailab\w*|food safety|human health)\b', re.IGNORECASE)


EXTRACTION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "minLength": 1, "description": "Copy ONE source surface entity, substance or trait name and abbreviations from the selected sentences, not ontology labels or expanded names. Never bundle 'X and Y' in one endpoint."},
                    "object": {"type": "string", "minLength": 1, "description": "Copy ONE source surface entity, substance or trait name and abbreviations from the selected sentences, not ontology labels or expanded names. Never bundle 'X and Y' in one endpoint."},
                    # Avoid a regex here: the live route constrained \\S to one
                    # character during decoding despite JSON Schema search semantics.
                    "relation": {"type": "string", "minLength": 2,
                                 "description": "One standalone source-faithful predicate linking subject to object, preserving negation/modality/comparison (e.g. 'increases', 'may increase', 'does not affect', 'is higher than'). Never put numbers/percentages (use qualifiers.metric), cultivar/comparator names (use endpoint_context or qualifiers.comparator), endpoint states ('low', 'high'), the subject, object, a causal chain or experimental conditions inside relation; never a single-letter code."},
                    "qualifiers": {
                        "type": "object",
                        "description": "Use only crop_or_species, population, condition, comparator and metric. Prefer omitting unused keys. Explicit null means no value, never an observation. Combine applicable details within string values; never create additional keys.",
                        "properties": {
                            "crop_or_species": {"type": ["string", "null"]},
                            "population": {"type": ["string", "null"]},
                            "condition": {"type": ["string", "null"], "description": "All applicable source-stated conditions and restrictions together in one string; preserve multiple details without adding subfields."},
                            "comparator": {"type": ["string", "null"]},
                            "metric": {"type": ["string", "null"]},
                        },
                        "additionalProperties": False,
                    },
                    "subject_anchor": {"type": "string", "description": "Shortest exact text span copied from the cited sentences naming the subject."},
                    "object_anchor": {"type": "string", "description": "Shortest exact text span copied from the cited sentences naming the object."},
                    "evidence_sentence_ids": {
                        "type": "array", "minItems": 1, "uniqueItems": True,
                        "items": {"type": "integer", "minimum": 1},
                    },
                    "subject_type": {"type": "string", "description": "Optional provisional hint; never a final ontology identity."},
                    "object_type": {"type": "string", "description": "Optional provisional hint; never a final ontology identity."},
                },
                "required": ["subject", "object", "relation", "qualifiers", "evidence_sentence_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["claims"],
    "additionalProperties": False,
}
