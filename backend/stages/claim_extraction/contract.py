"""Extraction response schema, limits and conservative lexical artifact guards."""

from __future__ import annotations

import re
from backend.shared.text_structure import REFERENCE_HEADING_TEXT
from backend.domain.ontology import ENTITY_TYPES


GENERIC_ENTITIES = {"agriculture", "food", "temperature", "yield", "disease", "soil", "pests", "crop", "health", "nutrition", "nutrient",
                    "crop growth", "crop productivity", "soil health", "soil quality", "nutrient levels",
                    "plantdisease", "soilcondition", "climatefactor", "humanexposure", "humanhealthoutcome", "agriculturalpractice"}


REFERENCE_SECTION_HEADING_RE = re.compile(
    rf"^\s*(?:\d+(?:\.\d+)*\.?\s+)?{REFERENCE_HEADING_TEXT}\s*$",
    re.IGNORECASE | re.MULTILINE,
)


EXTRACTION_MAX_CLAIMS_PER_PACKET = 0  # No application claim-count cap by default.


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
                    "claim_text": {"type": "string", "maxLength": 900},
                    "source_context": {"type": "string", "maxLength": 300},
                    "subject": {"type": "string"},
                    "subject_source_mention": {"type": "string"},
                    "relation": {"type": "string", "minLength": 1, "pattern": "\\S"},
                    "object": {"type": "string"},
                    "object_source_mention": {"type": "string"},
                    "relation_trigger": {"type": "string"},
                    "subject_type": {"type": "string", "enum": sorted(ENTITY_TYPES)},
                    "object_type": {"type": "string", "enum": sorted(ENTITY_TYPES)},
                    "evidence_sentence_numbers": {
                        "type": "array", "minItems": 1, "maxItems": 3, "uniqueItems": True,
                        "items": {"type": "integer", "minimum": 1},
                    },
                    "certainty": {
                        "type": "string",
                        "enum": ["causal", "associative", "conditional", "descriptive"],
                    },
                    "qualifiers": {
                        "type": "object",
                        "properties": {
                            "crop_or_species": {"type": "string"},
                            "population": {"type": "string"},
                            "condition": {"type": "string"},
                            "comparator": {"type": "string"},
                            "metric": {"type": "string"},
                        },
                        "required": ["crop_or_species", "population", "condition", "comparator", "metric"],
                        "additionalProperties": False,
                    },
                },
                "required": ["claim_text", "source_context", "subject", "subject_source_mention", "relation", "object", "object_source_mention",
                             "relation_trigger", "subject_type", "object_type",
                             "evidence_sentence_numbers", "certainty", "qualifiers"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["claims"],
    "additionalProperties": False,
}
