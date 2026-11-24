"""Single source of truth for the bounded, domain-specific extraction prompt."""

from backend.config.paths import EXTRACTION_PROMPT
from backend.stages.claim_extraction.contract import (
    CLOSED_PREDICATE_SET_VERSION,
    render_closed_predicate_glossary,
)


CLOSED_RELATION_ADDENDUM = f'''
# BOUNDED EXTRACTION ({CLOSED_PREDICATE_SET_VERSION})
Extract ONLY findings directly inside the ACTIVE APPLICATION SCOPE (soil phosphorus availability and microbial treatments, potato phosphorus uptake/growth/tuber phosphorus composition, food phosphate composition and dietary exposure, and human health outcomes linked to phosphate). If a passage only reports out-of-scope findings (such as correlations among Na, K, Ca, Mg, Zn, or Fe without phosphorus, standalone gene expression, or laboratory procedures), return {{"claims": []}}.

Each claim is subject → one approved object property (predicate_alias) → object. Use the glossary's definitions, endpoint requirements and upstream annotations to decide whether the target clause asserts that property. Wording can differ from the ontology label when the meaning is equivalent.

## Ontology assertions
- Select P1-P12 ONLY when the cited clause supports the full property, its direction, endpoint kinds and certainty. Omit unclear, borderline, or weakly supported claims rather than emitting incomplete ones. Put the cause first for a causal assertion.
- Distinguish causal target kinds cleanly:
  - Use P1/P2 when a process or intervention increases/decreases a characteristic or neutral measured variable (such as tuber phosphorus content, serum phosphate level, or parathyroid hormone level).
  - Use P11/P12 (`positively regulates` / `negatively regulates`) when a process or intervention increases/decreases the rate or magnitude of another biological process (such as phosphate ion transport, acid secretion, renal phosphate excretion, or calcitriol synthesis). Both subject and object must be processes.
  - Use P10 (`causes or contributes to condition`) when an explicit exposure, chemical, or biological factor causes or contributes to a disease or abnormal phenotype/condition (such as nephrocalcinosis, CKD progression, coronary artery calcification, or bone demineralization). Never use P10 for neutral physiological measurements or unestablished epidemiological associations.
  - Use P3 only between continuants; use P8 (`has input`) when the source identifies a food, nutrient, or food additive as an input to an intake or ingestion process (intake process in subject, food/additive in object). Do not force food-to-intake share estimates into causal P3.
- For passive causal wording put the stated cause first and keep the original passive wording in relation_anchor. When a supplied sentence explicitly links a finding to the named intervention, cite both. A group or organism name on its own does not stand for an intervention.
- Keep experimental conditions in qualifiers unless the target clause explicitly assigns them a causal role. "Trait Q decreased under Regime C" reports an observation and yields no causal assertion. "Treatment T decreased Trait Q under Regime C" supports a causal claim with Treatment T as subject and Regime C in qualifiers.condition.
- For P5 use the two measured variables. Copy the full reported cue into relation_anchor and preserve stated sign and significance in qualifiers.metric along with any reported statistic (for example, "significant negative correlation"). Preserve study and group context in qualifiers. A correlation that was not detected does not assert P5.
- For P6 identify the producer and the material it actually produces. Delivery, transfer, absorption and accumulation of existing material do not establish production.
- Effects stated as possible, undirected significance summaries, parallel trends, and standalone comparisons ("Group A had 12% lower Trait Q than Group B") supply no approved ontology assertion; return {{"claims": []}}.

## Clean canonical endpoints, qualifiers and anchors
- Express `subject` and `object` as short, self-contained **scientific noun phrases (2 to 6 words)** so equivalent concepts connect across papers (for example: `phosphorus supplementation`, `phosphorus deficiency`, `PGPR inoculation`, `root growth`, `root phosphorus concentration`, `phosphorus uptake`, `tuber phosphorus content`, `potato tuber yield`, `dietary phosphate intake`).
- NEVER put verb clauses, infinitives (`to reach...`), leading comparatives (`higher ... and ...`), inline treatment codes (`P30`, `CM1`, `T1`), inline numeric doses or units (`0 mg P L-1`, `>= 30 mg/L`, `15%`), or bundled `and` lists in `subject` or `object`. Expand bare element symbols (`P`, `Na`, `K`) to the actual nutrient/content concept indicated in the sentence (e.g., `phosphorus supplementation` or `tuber phosphorus content`).
- Place treatment codes, exact doses, exposure levels, organs (when contextual), and populations in `qualifiers.condition` and `qualifiers.population`; place baselines in `qualifiers.comparator`; place numeric values and significance in `qualifiers.metric`. Omit unused qualifier keys.
- Split independent outcomes into separate atomic claims. Preserve true joint causes, ratios, and chemical forms. For coordination sharing words, copy the full contiguous phrase in `subject_anchor` or `object_anchor`.
- Copy exact verbatim spans from the cited evidence for `subject_anchor`, `object_anchor`, and `relation_anchor` (with no ellipses). Every canonical `subject` and `object` must be faithfully grounded in its cited sentence and anchor.
- Supply subject, predicate_alias, object, qualifiers, ascending unique evidence_sentence_ids, subject_anchor, object_anchor, and relation_anchor. The source wording belongs in relation_anchor. There is no free-text relation field. Return {{"claims": []}} when no in-scope approved property fits.'''


def build_extraction_prompt(claims_per_packet: int, extraction_mode: str = 'whole_packet',
                            *, closed_relations: bool = False, reference=None) -> str:
    template = EXTRACTION_PROMPT.read_text(encoding='utf-8').strip()
    if not template:
        raise ValueError('The extraction prompt template is empty')
    if closed_relations:
        template = render_closed_predicate_glossary(reference) + '\n' + CLOSED_RELATION_ADDENDUM
    if extraction_mode == 'target_context':
        template += ('\nTARGET/CONTEXT CONTRACT: Extract only from extraction_target. '
                     'reading_context_only explains referents and conditions. Cite at least one target sentence; supplied reading-context IDs may support antecedents or restrictions, never a separate finding.')
    if closed_relations:
        limit = (f'Return at most {claims_per_packet} distinct findings. No target minimum.' if claims_per_packet
                 else 'Return eligible direct atomic findings in the assigned text. There is no numeric claim-count cap.')
    else:
        limit = (f'Return at most {claims_per_packet} distinct, high-value edges. No target minimum.' if claims_per_packet
                 else 'Return ALL eligible direct atomic edges in the assigned text. There is no numeric claim-count cap. '
                      'Do not omit a connected edge because other claims have already been extracted. Do not invent edges to increase count.')
    attention = ('Inspect only the explicitly marked extraction target.' if extraction_mode == 'target_context'
                 else 'Use the priority evidence index as an attention aid, then inspect the rest of the whole packet.')
    return template + '\n\n' + limit + '\n' + attention
