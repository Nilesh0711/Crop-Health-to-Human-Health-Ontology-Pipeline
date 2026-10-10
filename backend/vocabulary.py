"""Conservative entity normalization against established vocabulary identifiers.

Only exact labels and unambiguous aliases are linked automatically.  An
unmapped entity keeps its normalized local label and a recommended vocabulary;
the pipeline never invents an external identifier from fuzzy similarity.
"""

from __future__ import annotations

import re
from typing import Any


_CHEBI = {
    "cadmium": ("cadmium atom", "CHEBI:22977"),
    "cd": ("cadmium atom", "CHEBI:22977"),
    "arsenic": ("arsenic atom", "CHEBI:22632"),
    "as": ("arsenic atom", "CHEBI:22632"),
    "lead": ("lead atom", "CHEBI:25016"),
    "pb": ("lead atom", "CHEBI:25016"),
    "zinc": ("zinc atom", "CHEBI:27363"),
    "zn": ("zinc atom", "CHEBI:27363"),
    "iron": ("iron atom", "CHEBI:18248"),
    "fe": ("iron atom", "CHEBI:18248"),
    "selenium": ("selenium atom", "CHEBI:27568"),
    "se": ("selenium atom", "CHEBI:27568"),
}

_SCHEME_BY_TYPE = {
    "AgriculturalPractice": "AGROVOC",
    "ClimateFactor": "AGROVOC",
    "SoilMaterial": "AGROVOC",
    "SoilCondition": "AGROVOC",
    "SoilChemicalProperty": "AGROVOC",
    "SoilBiota": "AGROVOC",
    "Crop": "AGROVOC",
    "FoodProduct": "FOODON",
    "PlantDisease": "AGROVOC",
    "PlantPest": "AGROVOC",
    "PlantProcess": "AGROVOC",
    "PlantTrait": "AGROVOC",
    "Nutrient": "ChEBI",
    "Contaminant": "ChEBI",
    "HumanExposure": "MeSH",
    "HumanBiologicalProcess": "MeSH",
    "HumanHealthOutcome": "MeSH",
}


def _local_label(value: object) -> str:
    text = re.sub(r"[_-]+", " ", str(value or "").strip())
    return " ".join(text.split()).casefold()


def normalize_entity(value: object, entity_type: object) -> dict[str, Any]:
    """Return an auditable exact vocabulary mapping or an explicit unmapped state."""
    label = _local_label(value)
    scheme = _SCHEME_BY_TYPE.get(str(entity_type or ""), "local")
    result: dict[str, Any] = {
        "canonical_label": label,
        "vocabulary": scheme,
        "match_method": "local_normalization",
        "vocabulary_id": None,
        "uri": None,
    }
    if str(entity_type or "") in {"Nutrient", "Contaminant", "SoilChemicalProperty"} and label in _CHEBI:
        canonical_label, identifier = _CHEBI[label]
        result.update({
            "canonical_label": canonical_label,
            "vocabulary": "ChEBI",
            "match_method": "exact_alias",
            "vocabulary_id": identifier,
            "uri": f"https://purl.obolibrary.org/obo/{identifier.replace(':', '_')}",
        })
    return result

