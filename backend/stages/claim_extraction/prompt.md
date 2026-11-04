# OBJECTIVE & DOMAIN BOUNDARY
Extract direct scientific edges for a Soil Health → Crop Health → Human Health Knowledge Graph using ONLY the supplied text. Inspect all assigned text. Zero eligible claims is valid: if every statement is excluded or out-of-domain, return {"claims": []}.

Domain pathway:
Agricultural/Climate factors → Soil properties/microbiome/contaminants → Crop growth/plant pathogens/traits → Food composition/nutrients/contaminants → Human dietary intake/biological processes/health outcomes.

# CORE EXTRACTION PRIORITIES (HIGHEST FIRST)
1. Soil/crop/food → named nutrient, mineral, toxin, or contaminant (e.g., infected Crop C material -> has_contaminant -> Compound T).
2. Contaminant/nutrient dietary exposure → human biological process or health outcome.
3. Explicit agricultural, climate, soil, pathogen, or pest mechanisms altering crop composition, contamination, nutrient availability, food quality, or processing recovery.
4. Upstream biological mechanisms directly connecting soil health to crop traits/processes. Never invent a downstream human connection.

# STRICT NEGATIVE EXCLUSIONS (DO NOT EXTRACT)
- EXCLUDE: Pharmaceutical commercialization, antibiotic market statistics, drug screening history, or patent counts (e.g., 'X% of antibiotics marketed between 1982-2019 were natural products').
- EXCLUDE: Aggregate harvest tonnage, farm productivity/yield alone, market economics, exports, trade, or generic food security without a specific biological/composition mechanism.
- EXCLUDE: Macroscopic planetary statistics (e.g., 'Soil degradation affects 15% of Earth's land surface') without an explicit biological or chemical mechanism.
- EXCLUDE: Pure sediment/runoff transport into surface waterbodies unless explicitly connected to crop irrigation contamination or human drinking-water exposure.
- EXCLUDE: Animal toxicity (Animal toxicity is not a HumanHealthOutcome).
- EXCLUDE: Authorial commentary, research priorities, recommendations, figure/table captions, and model-fitting statistics.
- For mixed sentences, extract ONLY the eligible biological/food-quality edge and omit harvest tonnage/productivity alone.

# ABSTRACT SCOPE EXAMPLES (Generic placeholders: Crop C, Disease D, Compound T, Practice P)
- EXCLUDE: 'Annual production losses of Crop C total quantity Q.' This is aggregate harvest tonnage/productivity alone.
- EXCLUDE: 'Disease D reduced field yield of Crop C and exports fell.' Yield and trade only.
- EXCLUDE: 'Disease D threatens food security.' No specific biological or compositional mechanism.
- INCLUDE: 'Disease D -> infects -> Crop C material' when the text establishes direct biological infection.
- INCLUDE ONLY SAFETY EDGE: 'Disease D reduces yield of Crop C; material infected with Disease D may contain contaminant Compound T.' Extract infected Crop C material -> has_contaminant -> Compound T with infection qualifier; omit yield.
- NEVER COLLAPSE CHAINS: Never collapse A -> B -> C into A -> C. Extract only direct edges explicitly stated in the source.

# SCIENTIFIC ATOMICITY & DOMAIN ANCHORING
- MANDATORY DOMAIN ANCHOR: Every returned edge must anchor to at least one entity in the Soil, Crop, or Human Health domain. Never extract an edge where both endpoints are generic science or Unresolved.
- NO SELF-LOOPS: Subject and object must be distinctly different entities. Never output an entity causing itself.
- ONE DIRECT ATOMIC EDGE PER CLAIM: Split conjoined causes, treatments, or multi-symptom lists into separate independent claims.
  - Conjoined practices: 'Practice P1 and Practice P2 caused Disease D' yields two separate claims: Practice P1 -> causes -> Disease D, and Practice P2 -> causes -> Disease D.
  - Conjoined outcomes: 'causes Outcome O1 and Outcome O2' yields two separate atomic claims.
- EXACT WORDING ANCHORS: Copy subject_source_mention, object_source_mention, and relation_trigger as exact contiguous words from the evidence sentences.
- CANONICAL ENTITY NAMES: Use precise canonical names; resolve 'the pathogen', 'the toxin' or abbreviations only when defined in context. Never guess an undefined expansion.
- EVIDENCE SENTENCE IDs: 1 to 3 unique ascending sentence IDs from the extraction target proving this specific edge.
- CLAIM TEXT: One concise active sentence (20-45 words, maximum 80) restating ONLY this atomic proposition with essential restrictions. Never repeat downstream consequences.
- MODALITY & CERTAINTY: Map capability or disposition phrasing ('can produce', 'ability to', 'potential to') to certainty='descriptive'. Use 'causal' only for explicit active causal verbs ('causes', 'reduces', 'increases').
- RESTRICTIONS VERSUS PROVENANCE:
  - qualifiers holds actual restrictions: crop_or_species, cultivar, infection status, treatment, dose, comparator, and measured metric.
  - source_context holds the documented location, farming system, or sampling site where an occurrence was reported.
