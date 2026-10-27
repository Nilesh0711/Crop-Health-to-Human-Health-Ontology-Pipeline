Extract direct scientific edges for a soil-to-human-health knowledge graph using only the supplied paper text. Return the supplied JSON schema without commentary or tools. Inspect all of the assigned extraction text. This is evidence extraction, not a paper summary. Zero eligible claims is valid. Follow any explicitly configured claim cap; otherwise return all eligible direct edges you find.

Research priorities, highest first:
1. Soil/crop/food -> named nutrient or contaminant; exposure -> human biological/health outcome.
2. Explicit agricultural, climate, soil, disease or pest mechanisms altering composition, contamination, nutrient availability, food quality or processing recovery.
3. Other specific upstream biological mechanisms usable as pathway segments. Never invent a downstream human-health connection.

Use these priorities to organize eligible findings, not to discard their connected direct edges. For a mechanism, keep the actor actually named by the sentence, not its broader section topic.

Exclude economics, trade, infrastructure, generic food security, harvest tonnage/productivity alone, recommendations, research aims, figure/table captions and model-performance statistics. Food safety, composition, eating/cooking quality and milling/processing recovery are eligible. Do not infer nutrient loss or health effects from a quality change. Animal toxicity is not a HumanHealthOutcome.

Apply this domain boundary BEFORE returning an edge. The fact that a sentence occurs in an agricultural paper, has two entities, uses a causal verb, or names a crop is not sufficient. A numeric statistic is not a biological pathway. Do not output excluded claims for a later validator to discard. Inspect the whole target, but return an empty claims array when every statement is excluded. For a mixed sentence, extract its eligible biological/food-quality edge and omit its yield, price or trade edge. An eligible neighboring edge does not make a yield-only edge eligible.

Abstract scope examples (Crop C, Disease D, Compound T and Process P are placeholders, never facts or names to copy into output):
- EXCLUDE: 'Annual production losses of Crop C total quantity Q.' This is aggregate harvest tonnage; do not make losses -> totals -> quantity.
- EXCLUDE: 'Disease D reduced the field yield of Crop C and exports fell.' Both endpoints concern yield/trade alone, even though a disease is named.
- INCLUDE ONLY THE SAFETY EDGE: 'Disease D reduces the field yield of Crop C; material infected with Disease D may contain contaminant Compound T.' Extract infected Crop C material -> has_contaminant -> Compound T with the infection condition; omit the yield-only edge.
- INCLUDE: 'Disease D reduces Process P in Crop C.' Include when the supplied text identifies Process P as a specific biological process, rather than harvest productivity alone.
- INCLUDE: 'Processing recovery of Crop C decreases as its damaged-material count increases.' Preserve the descriptive trend and measured predictor.
- EXCLUDE: 'Disease D threatens food security.' No specific biological, composition or food-quality mechanism is stated.

Evidence and atomicity:
- One claim = one direct edge proved by 1-3 original numbered sentences, listed uniquely in ascending order. Non-adjacent IDs may identify fragments separated by a table or page break; gaps are retained explicitly for source review. Never skip conditions or opposing text to change meaning. The wider packet/local heading may resolve explicit references, not supply a missing mechanism. Never collapse A -> B -> C into A -> C.
- Copy subject_source_mention, object_source_mention and relation_trigger as exact contiguous words from those sentences. Endpoint anchors are noun phrases, without the predicate. Include the measured property, not just its organism/chemical. No ellipses or slash-joined fragments.
- Use specific canonical names. Resolve 'the pathogen', 'toxins' or abbreviations only from explicit local antecedents. Never substitute another crop, material, chemical, outcome or population. Split explicitly named contaminants into atomic edges; never invent class membership.
- Spell out abbreviations and abbreviated organism genera in canonical subject/object names, qualifiers, source_context and claim_text whenever the supplied context defines them. Abstract naming example: source 'Disease D (DD)' -> canonical 'Disease D', while its exact source mention may remain 'DD'. Apply the same source-defined expansion rule to compound names. Do not append the abbreviation in parentheses to the full canonical name. Never guess an undefined expansion or alter literal evidence, sentence IDs, source mentions, units, chemical formulae or gene/strain identifiers. Leave uncertain identity for source review.
- Capture each explicitly stated edge in a connected sequence, even when an endpoint is a pronoun or local reference. Abstract structure example: 'Disease D infects Crop C material. This infected material may contain contaminant Compound T.' yields Disease D -> infects -> Crop C material AND Crop C material -> has_contaminant -> Compound T. The second edge is conditional and retains condition='Disease D infection'. Use 'This infected material' as its exact source mention and resolve its canonical identity from the preceding sentence. Keep both original evidence spans; never replace these edges with Disease D -> Compound T.
- Choose a relation that faithfully expresses this edge. The common relation names are suggestions, not a closed set. For example, 'Pathogen P produces compound T' can yield P -> produces -> T; do not force production into disease/container containment. Keep any separately stated material-contaminant edge as well.
- Types are provisional labels, not eligibility rules. Distinguish disease conditions from pathogen organisms/strains, and crop traits from pathogen traits when the local evidence allows it. Use Unresolved rather than guessing a type. No subject/object type combination is automatically forbidden.
- Preserve modality: hedged or conditional wording remains conditional; observed directional findings can be descriptive; use causal only for explicit causal wording. Extract associations as associations and comparisons as comparisons, preserving comparator, measured metric, population and conditions. Never upgrade correlation to causation. Both are eligible within the project scope.

Restrictions versus provenance:
- qualifiers holds actual restrictions: crop/species, cultivar, infection status, treatment, dose, essential exposed population, comparator and measured metric. Do not duplicate endpoints.
- source_context holds the location, farming system or sampling site where an occurrence was reported. For descriptive has_contaminant occurrence edges, this is provenance, NOT a requirement that corroborating papers repeat that setting. The edge does not assert universal contamination.
- Do not duplicate a setting in condition/population and source_context. Retain actual infection, treatment/processing, dose and subgroup restrictions; never remove them merely to broaden a claim.
- Schema illustration, NOT a fact to extract: 'Crop R samples from farming sites in region S contained compound T' -> Crop R has_contaminant compound T, source_context='farming sites in region S', condition empty. 'Only treated Crop R samples contained compound T' retains condition='treated samples'.

claim_text is one short sentence, preferably 20-45 words (maximum 80), restating ONLY this edge with essential restrictions and modality. Describe occurrence context as 'reported in samples from ...', not a universal farming-system property. Add no inferred mechanism, downstream consequence or unsupported health outcome.

Represent the measured predictor, not the section topic: 'processing recovery decreases as infected-grain count increases' uses infected-grain count -> reduces -> processing recovery (PlantTrait -> PlantTrait, descriptive), not disease -> causes -> processing recovery.

Write claim_text from the returned endpoints and relation. A material-contaminant containment edge must say that the material contains/may contain the contaminant, not that its disease produces the contaminant. No extra causal interpretation or consequences.

Return all schema fields, including source_context (empty when absent). Check exact anchors, original sentence IDs, measured outcome and provenance/restriction separation once; then return JSON, without narrating the checks.
