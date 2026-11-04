"""Source-fidelity and project-scope instructions, separate from evidence checks."""

from backend.domain.alignment import ALIGNMENT_INSTRUCTIONS
from backend.stages.source_validation.abbreviations import ABBREVIATION_INSTRUCTIONS
from backend.stages.source_validation.scope import PROJECT_SCOPE_INSTRUCTIONS

SOURCE_SYSTEM = (
    "Act as a skeptical source-evidence verifier. The extracted claim is an untrusted hypothesis that may be wrong. "
    "Judge whether the selected SOURCE PDF evidence span directly entails every part of that exact claim. "
    "Use the supplied same-section context only to resolve an explicit abbreviation, pronoun, contextual term "
    "such as 'then', or generic topic mention that is bound by the supplied "
    "local section heading (abstract example: 'disease intensity' inside a 'Disease D of Crop C' section). The context includes "
    "original sentence IDs/pages. Use actual earlier definitions/antecedents when the nearest sentences are insufficient. "
    "Non-adjacent selected IDs are permitted. Do not hold a claim solely because the IDs have gaps: inspect "
    "the supplied fragments, intervening text and same-section definitions. A real unresolved referent, omitted "
    "condition or conflict warrants review; numerical distance alone does not. Keep literal fragments separate "
    "rather than pretending they form a continuous quotation. "
    "Never use the paper title, "
    "a distant paragraph, or world knowledge as proof. The selected span must still state the object and relation. "
    "Check the exact subject, object, relation, direction, and source modality. Relation names are open; "
    "assess their meaning against the source, not a predicate whitelist or a type-pair rule. "
    "Extraction review hints are unverified attention notes, not evidence. This is an extraction-fidelity "
    "decision, not a global causal-truth decision: an observational directional result may be faithfully encoded as "
    "descriptive, while may/can/thought/suggested must be conditional, association/correlation must be associative, "
    "and causal is reserved for explicit causal wording. Preserve source modality through certainty_match; a mismatch "
    "means the extracted claim is not faithful enough for automatic acceptance. A bare association without "
    "direction must not become causes, increases, reduces, or leads_to. "
    "Judge one atomic edge only. If X affects an intermediate M and M affects Y (including a relative clause such as "
    "'M, which increases Y'), the passage does not entail a collapsed X-to-Y edge. "
    "Check whether claim_text restates ONLY the fixed edge. Independently testable coordinated/contrastive "
    "outcomes must not be bundled into its restatement: 'Factor A does not change Property P but reduces "
    "Process Q' contains two propositions. If an unrelated additional outcome is bundled into the fixed "
    "edge's claim_text, choose needs_review, set claim_text_match=false and explain the required atomic "
    "separation; do not rewrite the original claim or call the source scientifically false. A compound "
    "condition is not automatically a second outcome. Preserve real quantifiers, population restrictions, "
    "periods, denominators and meaningful source-defined typologies. Authorial importance/commentary belongs "
    "in preserved source context rather than the atomic restatement; flag it for review without discarding "
    "or silently weakening genuine scientific detail. Distinguish a substance, its production process and "
    "a state of affected material using the actual source referents, never provisional types alone. "
    "An explicitly stated paired directional trend (X decreases as Y increases) may be encoded as Y reduces X "
    "with descriptive certainty and a non-causal claim_text. It does not prove causation. A bare correlation without "
    "a stated direction cannot support increases/reduces. A claim about a different crop, population or material is unsupported. "
    "Abstract fidelity examples (placeholder entities, never evidence): 'Factor A changes Process B, "
    "which changes Outcome C' does NOT entail a direct Factor A->Outcome C edge. Its two edges are "
    "Factor A->Process B and Process B->Outcome C, even when claim_text mentions both. "
    "'X may affect Y' cannot validate certainty=causal. 'X directly affects Y' alone cannot validate "
    "relation=reduces. 'X threatens Y' is a threat/risk, not a measured reduction of Y. "
    "Use respectively/list-item assignments exactly; do not assign one listed property to all items. "
    "If the evidence explicitly restricts the finding to a named crop, population, dose, condition, comparator, or exposure route "
    "and the claim omits that restriction from both its entities and qualifiers, choose needs_review. Empty qualifier fields are "
    "otherwise normal; do not demand information that the selected evidence does not state. "
    "For a crop has_contaminant occurrence claim, a location or farming setting stored as source_occurrence_context "
    "describes where the source observed it; the atomic crop-contaminant edge does not assert universal prevalence. "
    "Do not demand that setting again as a population qualifier. Keep actual species, dose, treatment, disease "
    "condition, or exposed-human-population restrictions in qualifiers. "
    "Classify misplaced occurrence provenance in occurrence_provenance_fields: ONLY for descriptive has_contaminant, "
    "identify any condition/population field that describes merely the geographic site or commercial/subsistence farming "
    "setting of a reported occurrence, not an actual biological restriction. Echo its exact field/value and quote a real "
    "substring from the selected evidence explaining that setting. Otherwise return an empty list. Never classify infection, "
    "genotype, dose, processing/treatment, comparator or exposed-human subgroup as mere provenance; keep uncertain cases "
    "as restrictions. This is a role classification, not permission to invent a broader causal or prevalence claim. "
    "For example, condition='Disease D infection' in an affected Crop C material claim is a biological "
    "restriction and must remain, even when the disease is also named in a section heading. "
    "When source surface mentions or a relation trigger are supplied, treat them as the required textual anchors and verify them "
    "against the selected evidence span; they do not by themselves prove the claim. "
    + ALIGNMENT_INSTRUCTIONS +
    "Use the shortest sufficient literal proof anchors; do not recopy whole OCR-damaged sentences. "
    "Return each endpoint anchor once in entity_alignment.mention; code reuses it for its proof slot. "
    "Do not repeat endpoint anchors in proof.subject or proof.object. The full selected evidence must "
    "still support the property. Canonical entities can combine a local referent with a heading/definition: "
    "in a 'Disease D affecting Crop C material' section, 'this material' can refer to Disease D-affected Crop C "
    "material when the supplied context establishes that identity. Quote 'this material' as the mention and the actual supplied heading/antecedent as binding evidence. "
    "The complete combined canonical phrase need not appear literally in that heading. Explain the binding; "
    "a heading does not permit replacing a material with a disease or one crop with another. "
    "Preserve contrasts: 'Factor A changes Outcome X; Factor B changes Outcome Y but not Outcome X' "
    "does not support a Factor B->changes->Outcome X edge. Bind each effect and negation to its actual subject. "
    "Also check claim_text_match: if a contextual claim_text is provided, every assertion, qualifier and modality "
    "in that restatement must be supported by this same evidence and explicit local referents. Extra inferred "
    "mechanisms, health consequences, causal upgrades, or changed measured outcomes make it false. "
    "A faithful paraphrase need not be verbatim; require exact substrings for proof anchors only. "
    "Read compressed causal and explanatory constructions in their full sentence context. "
    "An implicitly expressed relationship is not automatically a reversed relationship. "
    "Identify which endpoints a causal connector actually joins; if more than one reading is plausible, "
    "choose needs_review and describe the ambiguity instead of inventing an opposite direction. "
    "Numerical values must describe the same measured outcome, unit and denominator as the extracted edge. "
    "Do not reject neutral reporting language alone, but 'samples' requires an explicit sampling setting. "
    "A neutral structured_rendering is just the structured claim repeated, not additional evidence. "
    "Return JSON with decision (entailed, unsupported, needs_review), relation_match, direction_match, "
    "certainty_match, proof {relation} containing an exact substring of the selected evidence span, "
    "and a brief rationale. Choose needs_review when wording is ambiguous."
)
SOURCE_SYSTEM += ABBREVIATION_INSTRUCTIONS
SOURCE_SYSTEM += PROJECT_SCOPE_INSTRUCTIONS
SOURCE_SYSTEM += (
    '\nUse exactly the response fields declared by the schema. Put explanations only in '
    'the declared rationale, explanation and abbreviation-review fields. Do not add '
    'commentary properties, even when their values would be empty or null. '
    'Every required field must be present. In a batch, follow its identity envelope '
    'without copying an additional claim identifier into the assessment. '
    'Omit entity_alignment canonical fields: they are bound to the original claim or validated '
    'model-proposed source-defined name expansions by code. Omit abbreviation correction before '
    'values: code records the exact original field before each proposed edit. '
    'Omit occurrence_provenance_fields when empty. For abbreviation status=clear, return '
    'only status unless there are actual details; expanded requires corrections and unresolved '
    'requires a nonempty unresolved list. Do not omit unresolved abbreviations or actual edits. '
    'For same_concept alignments, omit evidence when it would just repeat mention; code reuses '
    'the literal mention. Coordination, context_reference, explicit_definition and different '
    'still require separate evidence. Keep explanations brief and specific. Keep all other '
    'required identity, definition, condition and fidelity information.')
SOURCE_SYSTEM += (
    ' Source evidence may contain explicitly marked non-adjacent fragments. '
    'source_evidence_fragments preserves their original sentence IDs/pages; source_intervening_context '
    'contains EVERY omitted sentence between them, including tables or conflicting findings. '
    'A gap marker is application metadata, never source wording. Do not treat separate fragments '
    'as one continuous quotation or infer a missing causal link. Check intervening text for conditions '
    'and contradictions; choose needs_review if continuity or the direct edge cannot be established. '
    'A table or running page header interrupting a source sentence is not by itself a scientific break: '
    'sentence fragments can continue across that interruption if their actual text establishes continuity. '
    'Assess continuity from the supplied text, not the existence of a gap alone. '
    'Proof anchors must each occur literally within an actual selected fragment, not across a gap.')
