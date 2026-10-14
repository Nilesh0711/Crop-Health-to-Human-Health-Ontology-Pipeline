"""Controlled vocabulary for the soil-to-human-health knowledge graph.

Semantic compatibility is judged from evidence by Qwen. This module only
enforces stable labels so malformed model output cannot enter the pipeline.
"""

ENTITY_DEFINITIONS = {
    "AgriculturalPractice": "A deliberate farming or land-management intervention.",
    "ClimateFactor": "A weather or climate condition affecting the agricultural system.",
    "SoilCondition": "An abiotic soil property, state, or process.",
    "SoilMaterial": "Soil, a soil fraction, amendment, or soil-associated physical material.",
    "SoilChemicalProperty": "A soil chemical concentration, availability, pH state, or chemical composition property.",
    "SoilBiota": "A living soil microorganism, microbial community, fungus, or soil fauna.",
    "Crop": "A cultivated plant, harvested crop, or edible crop material.",
    "FoodProduct": "An edible food, ingredient, or processed food product derived from agricultural material.",
    "PlantDisease": "A named plant infection or disease condition.",
    "PlantPest": "An organism that damages a crop or plant.",
    "PlantProcess": "A biological plant process such as uptake, resistance, or root development.",
    "PlantTrait": "A measurable crop or food characteristic such as composition, quality, or resistance level.",
    "Nutrient": "A beneficial nutrient, nutrient concentration, or nutritional component.",
    "Contaminant": "A harmful chemical, heavy metal, toxin, or mycotoxin.",
    "HumanExposure": "Human intake, ingestion, contact, absorption, or another exposure route.",
    "HumanBiologicalProcess": "A human biomarker, immune response, metabolic process, or epigenetic change.",
    "HumanHealthOutcome": "A human disease, symptom, physiological effect, or health outcome.",
}

RELATION_DEFINITIONS = {
    "affects": "Changes or influences without asserting a specific direction.",
    "causes": "Directly produces the object outcome.",
    "reduces": "Decreases, suppresses, or protects against the object.",
    "increases": "Raises, promotes, or enhances the object.",
    "has_nutrient": "Contains the stated nutrient or nutritional component.",
    "has_contaminant": "Contains or carries the stated harmful chemical, toxin, or contaminant.",
    "contains": "Contains the object when its nutrient, contaminant, or trait role is not established.",
    "has_trait": "Exhibits the stated measurable plant, crop, food, or biological trait.",
    "higher_than": "Has a higher value of the explicitly qualified metric than the comparison object.",
    "lower_than": "Has a lower value of the explicitly qualified metric than the comparison object.",
    "leads_to": "Produces a downstream result explicitly stated in the evidence.",
    "associated_with": "Has a non-causal association or correlation with the object.",
    "deficiency_causes": "A deficiency explicitly produces the health outcome.",
}

ENTITY_TYPES = set(ENTITY_DEFINITIONS)
RELATIONS = set(RELATION_DEFINITIONS)
# Automatic extraction begins with direct, typed pathway segments. Causal
# predicates, comparisons, and observational associations remain available in
# the wider ontology for curated/legacy claims and later specialised workflows,
# but are deliberately excluded from the starter graph: they require study
# design, population, exposure, and comparator modelling not yet represented.
AUTOMATIC_EXTRACTION_RELATIONS = frozenset({
    "affects", "reduces", "increases", "has_nutrient", "has_contaminant",
    "contains", "has_trait",
})

PATHWAY_DESCRIPTION = (
    "Agriculture or climate -> soil -> crop, plant disease, pest, plant process, or plant trait -> "
    "nutrient or contaminant -> human exposure -> human biological process or health outcome. Direct links "
    "may skip stages when explicitly stated, but economics, infrastructure, trade, "
    "generic food security, and yield-only outcomes are outside scope."
)

# This is intentionally a small, conservative *structural* gate.  It does not
# try to decide scientific truth (Qwen and the evidence judges do that); it
# prevents edges that cannot be a segment of the project pathway from spending
# source-entailment and external-retrieval budget.  A new pair should be added
# only when it has a clear pathway interpretation.
PATHWAY_TYPE_PAIRS: dict[str, set[str]] = {
    "AgriculturalPractice": {"SoilMaterial", "SoilCondition", "SoilChemicalProperty", "SoilBiota", "Crop", "FoodProduct", "PlantProcess", "PlantTrait", "PlantDisease", "PlantPest", "Nutrient", "Contaminant", "HumanExposure", "HumanBiologicalProcess", "HumanHealthOutcome"},
    "ClimateFactor": {"SoilMaterial", "SoilCondition", "SoilChemicalProperty", "SoilBiota", "Crop", "FoodProduct", "PlantProcess", "PlantTrait", "PlantDisease", "PlantPest", "Nutrient", "Contaminant", "HumanExposure", "HumanBiologicalProcess", "HumanHealthOutcome"},
    "SoilMaterial": {"SoilCondition", "SoilChemicalProperty", "SoilBiota", "Crop", "FoodProduct", "PlantProcess", "PlantTrait", "PlantDisease", "PlantPest", "Nutrient", "Contaminant", "HumanExposure", "HumanBiologicalProcess", "HumanHealthOutcome"},
    "SoilCondition": {"SoilMaterial", "SoilChemicalProperty", "SoilBiota", "Crop", "FoodProduct", "PlantProcess", "PlantTrait", "PlantDisease", "PlantPest", "Nutrient", "Contaminant", "HumanExposure", "HumanBiologicalProcess", "HumanHealthOutcome"},
    "SoilChemicalProperty": {"SoilMaterial", "SoilCondition", "SoilBiota", "Crop", "FoodProduct", "PlantProcess", "PlantTrait", "PlantDisease", "PlantPest", "Nutrient", "Contaminant", "HumanExposure", "HumanBiologicalProcess", "HumanHealthOutcome"},
    "SoilBiota": {"SoilMaterial", "SoilCondition", "SoilChemicalProperty", "Crop", "FoodProduct", "PlantProcess", "PlantTrait", "PlantDisease", "PlantPest", "Nutrient", "Contaminant", "HumanExposure", "HumanBiologicalProcess", "HumanHealthOutcome"},
    "Crop": {"Crop", "FoodProduct", "PlantProcess", "PlantTrait", "PlantDisease", "PlantPest", "Nutrient", "Contaminant", "HumanExposure", "HumanBiologicalProcess", "HumanHealthOutcome"},
    "FoodProduct": {"PlantTrait", "Nutrient", "Contaminant", "HumanExposure", "HumanBiologicalProcess", "HumanHealthOutcome"},
    "PlantDisease": {"PlantTrait", "Contaminant", "Crop", "FoodProduct", "AgriculturalPractice"},
    "PlantPest": {"PlantTrait", "Contaminant", "Crop", "FoodProduct", "AgriculturalPractice"},
    # Root growth and other plant processes can directly alter physical soil
    # states such as porosity, aeration, and aggregate stability.
    "PlantProcess": {"SoilCondition", "SoilChemicalProperty", "PlantTrait", "Nutrient", "Contaminant"},
    "PlantTrait": {"Nutrient", "Contaminant"},
    # Nutrient and contaminant levels may alter crop traits before their
    # downstream human-exposure effects, so both plant-facing and
    # human-facing segments are retained.
    "Nutrient": {"Crop", "PlantProcess", "PlantTrait", "HumanExposure", "HumanBiologicalProcess", "HumanHealthOutcome"},
    "Contaminant": {"SoilCondition", "SoilChemicalProperty", "SoilBiota", "Crop", "PlantProcess", "PlantTrait", "HumanExposure", "HumanBiologicalProcess", "HumanHealthOutcome"},
    "HumanExposure": {"Contaminant", "Nutrient", "HumanBiologicalProcess", "HumanHealthOutcome"},
    "HumanBiologicalProcess": {"HumanHealthOutcome"},
}

_COMPARATIVE_RELATIONS = {"higher_than", "lower_than"}
_COMPOSITION_RELATIONS = {"has_nutrient", "has_contaminant", "contains"}


def pathway_error(candidate: dict, *, subject_type: str | None = None,
                  object_type: str | None = None) -> str | None:
    """Return a non-scientific pathway/shape error, if one is certain.

    This is deliberately narrower than a large hand-built ontology: it rejects
    clear category errors (for example, a farm programme -> programme trait or
    a contaminant -> contaminant edge), while leaving nuanced biological
    interpretation to the semantic evidence gate.
    """
    source_type = subject_type or candidate.get("subject_type")
    target_type = object_type or candidate.get("object_type")
    relation = candidate.get("relation")
    qualifiers = candidate.get("qualifiers") or {}
    if target_type not in PATHWAY_TYPE_PAIRS.get(source_type, set()):
        return f"Type pair {source_type} -> {target_type} is outside the accepted soil-to-human-health pathway"
    composition_sources = {"SoilMaterial", "SoilCondition", "SoilChemicalProperty", "Crop", "FoodProduct"}
    if relation in _COMPOSITION_RELATIONS and not (source_type in composition_sources and target_type in {"Nutrient", "Contaminant"}):
        return f"{relation} requires a soil, crop, or food material and a nutrient or contaminant"
    if relation == "has_trait" and not (source_type in {"Crop", "FoodProduct"} and target_type == "PlantTrait"):
        return "has_trait is reserved for a crop material or variety and a measurable plant trait"
    if relation in _COMPARATIVE_RELATIONS:
        if not str(qualifiers.get("comparator") or "").strip() or not str(qualifiers.get("metric") or "").strip():
            return f"{relation} requires both comparator and metric qualifiers"
        if source_type not in {"Crop", "FoodProduct"} or target_type not in {"Crop", "FoodProduct"}:
            return f"{relation} is reserved for a comparison between crop or food materials"
    return None


def schema_error(candidate: dict) -> str | None:
    """Reject malformed labels; Qwen handles semantic type compatibility later."""
    if candidate.get("subject_type") not in ENTITY_TYPES or candidate.get("object_type") not in ENTITY_TYPES:
        return "Unknown entity type"
    if candidate.get("relation") not in RELATIONS:
        return "Unknown relation"
    return None
