"""Controlled vocabulary for the soil-to-human-health knowledge graph.

Semantic compatibility is judged from evidence by Qwen. This module only
enforces stable labels so malformed model output cannot enter the pipeline.
"""

ENTITY_DEFINITIONS = {
    "AgriculturalPractice": "A deliberate farming or land-management intervention.",
    "ClimateFactor": "A weather or climate condition affecting the agricultural system.",
    "SoilCondition": "An abiotic soil property, state, or process.",
    "SoilBiota": "A living soil microorganism, microbial community, fungus, or soil fauna.",
    "Crop": "A cultivated plant, harvested crop, or edible crop material.",
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
    "leads_to": "Produces a downstream result explicitly stated in the evidence.",
    "associated_with": "Has a non-causal association or correlation with the object.",
    "deficiency_causes": "A deficiency explicitly produces the health outcome.",
}

ENTITY_TYPES = set(ENTITY_DEFINITIONS)
RELATIONS = set(RELATION_DEFINITIONS)

PATHWAY_DESCRIPTION = (
    "Agriculture or climate -> soil -> crop, plant disease, pest, plant process, or plant trait -> "
    "nutrient or contaminant -> human exposure -> human biological process or health outcome. Direct links "
    "may skip stages when explicitly stated, but economics, infrastructure, trade, "
    "generic food security, and yield-only outcomes are outside scope."
)


def schema_error(candidate: dict) -> str | None:
    """Reject malformed labels; Qwen handles semantic type compatibility later."""
    if candidate.get("subject_type") not in ENTITY_TYPES or candidate.get("object_type") not in ENTITY_TYPES:
        return "Unknown entity type"
    if candidate.get("relation") not in RELATIONS:
        return "Unknown relation"
    return None
