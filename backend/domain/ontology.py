"""Project vocabulary and scope guidance; scientific compatibility is evidence judged."""

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
    "PlantPathogen": "An organism, isolate, strain or pathotype causing plant disease; distinct from the disease condition.",
    "PathogenTrait": "A measured pathogen characteristic such as aggressiveness, virulence or thermal adaptation.",
    "PlantPest": "An organism that damages a crop or plant.",
    "PlantProcess": "A biological plant process such as uptake, resistance, or root development.",
    "PlantTrait": "A measurable crop or food characteristic, including composition, quality, infection burden, milling/processing recovery, or resistance level; not the food material itself.",
    "Nutrient": "A beneficial nutrient, nutrient concentration, or nutritional component.",
    "Contaminant": "A harmful chemical, heavy metal, toxin, or mycotoxin.",
    "HumanExposure": "Human intake, ingestion, contact, absorption, or another exposure route.",
    "HumanBiologicalProcess": "A human biomarker, immune response, metabolic process, or epigenetic change.",
    "HumanHealthOutcome": "A human disease, symptom, physiological effect, or health outcome.",
    "Unresolved": "A provisional entity whose contextual type cannot yet be established; requires external evidence review.",
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
# Common predicates are naming guidance, not an extraction/admission whitelist.

SCOPE_POLICY = (
    "Include direct biological mechanisms and crop/food composition, safety, cooking/eating quality, "
    "and processing or milling recovery as upstream pathway segments, even without a demonstrated human outcome. "
    "Plant-pathogen traits and adaptation are eligible when explicitly connected to a crop disease or crop/food mechanism. "
    "A relation name alone never establishes relevance: unrelated medicine, organism traits or general science are outside scope. "
    "Processing recovery is a food-material quality characteristic, not generic farm yield. "
    "Exclude aggregate harvest tonnage, farm productivity alone, economics, infrastructure, trade, "
    "and generic food availability/security without a biological or food-quality mechanism. "
    "Do not infer nutrient loss, toxicity, human exposure, or health effects from a quality or recovery change."
)

PATHWAY_DESCRIPTION = (
    "Agriculture or climate -> soil -> crop, plant disease, plant pathogen and its traits, pest, plant process, or plant trait -> "
    "nutrient or contaminant -> human exposure -> human biological process or health outcome. Direct links "
    "may skip stages when explicitly stated. " + SCOPE_POLICY
)

def schema_error(candidate: dict) -> str | None:
    """Validate label structure only; types are provisional and relations are open.

    The label vocabulary supports stable graph IDs. It does not decide whether
    an entity actually has that role, whether a type pair is plausible, or
    whether a claim belongs to the project. Those are evidence-based decisions.
    """
    if candidate.get("subject_type") not in ENTITY_TYPES or candidate.get("object_type") not in ENTITY_TYPES:
        return "Unknown entity label; use Unresolved when classification is uncertain"
    relation = candidate.get("relation")
    if not isinstance(relation, str) or not relation.strip():
        return "Relation must be a nonempty string"
    return None


def external_project_scope(result: dict) -> bool | None:
    """Read the latest completed model scope assessment, without inferring scope."""
    for attempt in reversed(result.get('attempts') or []):
        value = ((attempt.get('joint_assessment') or {}).get('semantic_assessment') or {}).get('in_scope')
        if isinstance(value, bool):
            return value
    return None
