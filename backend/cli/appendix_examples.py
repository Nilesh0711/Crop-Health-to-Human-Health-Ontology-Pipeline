"""Generate offline Appendix examples from the active prompts and catalog.

Run explicitly with ``python -m backend.cli.appendix_examples`` after prompt changes.
No provider calls, scientific decisions, or saved-run writes are performed.
"""
import json
from copy import deepcopy
from pathlib import Path

from backend.ontology.catalog import catalog
from backend.stages.claim_extraction.contract import EXTRACTION_CONTRACT_VERSION, verify_closed_predicates
from backend.stages.claim_extraction.prompt import build_extraction_prompt
from backend.stages.ontology_grounding.contract import ENTITY_SYSTEM, SYSTEM
from backend.stages.ontology_grounding.grounder import GROUNDING_POLICY, catalog_term_map
from backend.stages.ontology_grounding.rows import VERSION as TSV_VERSION
from backend.domain.classification import CLASSIFICATION_POLICY
from backend.ontology.usage import ANNOTATION_POLICY, USAGE_POLICY, ANNOTATION_PROJECTION_POLICY
from backend.stages.ontology_grounding.source_context import POLICY as DEFINITION_CONTEXT_POLICY


def examples():
    ref, terms, term_iris, relations, relation_iris = catalog_term_map(catalog())
    source = 'A root is part of a root system.'
    claim = {'subject': 'root', 'object': 'root system', 'relation': 'is part of',
             'qualifiers': {}, 'subject_anchor': 'root', 'object_anchor': 'root system', 'evidence_sentence_ids': [1]}
    extraction = {'stageName': f'Claim extraction (Contract v{EXTRACTION_CONTRACT_VERSION})',
        'description': 'Illustrative examples, not saved findings. Preserve meaning before ontology alignment.',
        'systemPrompt': build_extraction_prompt(0, 'target_context'),
        'input': 'Target sentence IDs: [1]\nSupport-only sentence IDs (never extract separate findings): []\n'
                 'Allowed evidence sentence IDs: [1]\n<extraction_target>\n[1] [page 1] ' + source + '\n</extraction_target>',
        'output': {'claims': [claim]}}
    root = next(k for k, iri in term_iris.items() if iri.endswith('/PO_0009005'))
    system = next(k for k, iri in term_iris.items() if iri.endswith('/PO_0025025'))
    part = next(k for k, iri in relation_iris.items() if iri.endswith('/BFO_0000050'))
    grounding = {'stageName': f'Ontology grounding ({GROUNDING_POLICY})',
        'versionNote': f'Catalogue profile v{ref.profile["version"]}; {TSV_VERSION}; {CLASSIFICATION_POLICY}; {ANNOTATION_POLICY}; {ANNOTATION_PROJECTION_POLICY}; {USAGE_POLICY}; {DEFINITION_CONTEXT_POLICY}.',
        'description': f'{len(terms)} curated classes and a searchable ontology inventory. Lexical retrieval supplies up to 14 class choices per endpoint. '
                       f'Each request handles one claim and sees up to 10 lexical matches from {len(ref.profile["allowed_object_properties"])} established properties. '
                       'Mapping identifies meanings; it does not establish scientific truth.',
        'systemPrompt': SYSTEM,
        'input': {'scope': {k: ref.profile[k] for k in ('id', 'crop', 'research_question', 'scope_description')},
                  'terms': {root: terms[root], system: terms[system]}, 'relations': {part: relations[part]}, 'reading_contexts': {},
                  'claims': {'C1': {k: claim[k] for k in ('subject', 'relation', 'object')} | {
                      'source_evidence_span': source, 'subject_candidates': [root], 'object_candidates': [system], 'relation_candidates': [part]}}},
        'output': {'results': [{'claim_id': 'C1', 'scope': 'in_scope',
            'scope_reason': 'Plant anatomical relationships fit the selected crop scope.',
            'subject_denotation': 'concept', 'object_denotation': 'concept',
            'subject': {'reason': 'The generic structure matches the supplied root concept.', 'mapping': 'exact', 'term': root},
            'object': {'reason': 'The generic structure matches the supplied root system concept.', 'mapping': 'exact', 'term': system},
            'relation': {'reason': 'The source states the supplied part-whole relationship.', 'mapping': 'exact', 'term': part}}]},
        'aliasExplanation': f'{root} -> PO:0009005 root; {system} -> PO:0025025 root system; '
                            f'{part} -> BFO:0000050 part of.'}
    atomic = {'source': 'Treatment T increased Trait Q by 12% and Trait R by 5% in Crop C seedlings compared with untreated seedlings.',
        'output': {'claims': [{'subject': 'Treatment T', 'relation': 'increased', 'object': trait,
            'qualifiers': {'crop_or_species': 'Crop C', 'population': 'seedlings',
                           'comparator': 'untreated seedlings', 'metric': metric},
            'subject_anchor': 'Treatment T', 'object_anchor': trait, 'evidence_sentence_ids': [1]}
            for trait, metric in [('Trait Q', '12%'), ('Trait R', '5%')]]}}
    roles = [
        {'source': 'Treatment T elevated Trait Q to 7 mg/g after 14 days.',
         'claim': {'subject': 'Treatment T', 'relation': 'elevated', 'object': 'Trait Q',
                   'qualifiers': {'metric': '7 mg/g', 'condition': 'measured after 14 days'},
                   'evidence_sentence_ids': [1]},
         'note': 'Values and times describe the measurement; they are not scientific entity endpoints. Pure spans receive a review hold.'},
        {'source': 'Despite Process X, Group A performed Process Y, promoting Trait R.',
         'claim': {'subject': 'Process Y', 'relation': 'promoting', 'object': 'Trait R',
                   'qualifiers': {'population': 'Group A'}, 'evidence_sentence_ids': [1]},
         'note': 'The governing action supplies the mechanism; the concessive process is not substituted as cause. Unsupported reconstructions remain omitted or held.'},
        {'source': 'Trait Q and Trait R were affected by Treatment T.',
         'claim': [{'subject': 'Treatment T', 'relation': 'affected', 'object': trait,
                    'qualifiers': {}, 'subject_anchor': 'Treatment T', 'object_anchor': trait,
                    'evidence_sentence_ids': [1]} for trait in ('Trait Q', 'Trait R')],
         'note': 'Two independently reported outcomes become separate claims; passive roles are preserved.'},
        {'source': 'Allocation of Compound Q supports Trait R in Group A.',
         'claim': {'subject': 'Allocation of Compound Q', 'relation': 'supports', 'object': 'Trait R',
                   'qualifiers': {'population': 'Group A'}, 'evidence_sentence_ids': [1]},
         'note': 'The stated mechanism is the subject; Group A remains population context.'},
        {'source': 'Trait Q declined as Factor T increased.',
         'claim': {'subject': 'Trait Q', 'relation': 'declined as', 'object': 'Factor T',
                   'qualifiers': {'condition': 'Factor T increased'}, 'evidence_sentence_ids': [1]},
         'note': 'Trait Q declined. This trend does not assert that Factor T caused the decline.'},
        {'source': 'Treatment T increased Compound Q concentration and content in Group A.',
         'claim': {'subject': 'Treatment T', 'relation': 'increased', 'object': 'Compound Q content',
                   'qualifiers': {'population': 'Group A'}, 'subject_anchor': 'Treatment T',
                   'object_anchor': 'Compound Q concentration and content', 'evidence_sentence_ids': [1]},
         'note': 'The complete literal coordination anchors the shared words. The concentration is a separate outcome.'},
        {'source': '[1] Compound Q accumulated in the sample. [2] It affected Trait R.',
         'claim': {'subject': 'Compound Q', 'relation': 'affected', 'object': 'Trait R',
                   'qualifiers': {}, 'subject_anchor': 'Compound Q', 'object_anchor': 'Trait R',
                   'evidence_sentence_ids': [1, 2]},
         'note': 'Sentence 2 is the target finding; sentence 1 supplies the literal antecedent. Ambiguous references remain unsupported.'},
    ]
    alignment_examples = []
    alignment_examples.append({'source':'A root is part of a root system.',
        'property_guidance':relations[part],
        'note':'Upstream inverse links describe the linked property with subject/object roles exchanged. Missing literal definitions remain absent.'})
    concentration = next(k for k, iri in term_iris.items() if iri.endswith('/PATO_0000033'))
    alignment_examples.append({'source': 'Material Q levels were adjusted to 1 and 2 mg/L in solution, labelled as Q1 and Q2.',
        'input': {'definition_contexts': {'definition-example': {'document': 'illustrative-document',
            'packet_number': 1, 'sentence_number': 2, 'page': 1,
            'text': 'Material Q levels were adjusted to 1 and 2 mg/L in solution, labelled as Q1 and Q2.',
            'selection_basis': 'variable_setup'}},
            'claims': {'C1': {'object': 'Material Q levels', 'object_definition_context_ids': ['definition-example']}}},
        'endpoint_alignment': {'reason': 'The source describes a concentration quality; the generic quality omits material specificity.',
                               'mapping': 'broader', 'term': concentration},
        'note': f'Illustrative only: {concentration} -> PATO:0000033 concentration of. Source descriptions clarify meaning; they do not rewrite the claim or establish another finding. Experimental manipulation is not an exposure-event identity.'})
    alignment_examples.append({'source': 'A measured concentration changes during an experiment.',
        'endpoint_alignment': {'mapping': 'unresolved', 'term': None,
                               'reason': 'No supplied definition faithfully encompasses this measured quality.'},
        'note': 'Illustrative outcome, not a vocabulary verdict: manipulation does not turn a concentration quality into an exposure event. A broader relation must not introduce ordering or statistical dependence.'})
    grouping = next(k for k, iri in relation_iris.items() if iri.endswith('/RO_0002410'))
    alignment_examples.append({'source': 'A source-stated causal relationship with more detail than a generic grouping.',
        'property_guidance': relations[grouping],
        'note': f'{grouping} -> RO:0002410 is grouping-only. Upstream notes permit broader classification, never exact/direct use. Comments do not replace definitions.'})
    for wording, mapping, reason in [
        ('is directly part of', 'broader', 'The supplied part-of property omits the stated directness.'),
        ('may be part of', 'unresolved', 'The supplied property cannot encode a possible part relationship.'),
    ]:
        output = deepcopy(grounding['output'])
        output['results'][0]['relation'] = {'reason': reason, 'mapping': mapping,
                                         'term': part if mapping == 'broader' else None}
        alignment_examples.append({'source': 'A root ' + wording + ' a root system.',
            'original_predicate': wording, 'output': output,
            'note': 'Illustrative alignment only. Broader is classified, not reasoning-ready; modal wording is preserved.'})
    trait = next(k for k, iri in term_iris.items() if iri.endswith('/TO_0000387'))
    alignment_examples.append({'source': 'A specialized plant trait was observed.',
        'endpoint_alignment': {'reason': 'The generic plant-trait definition encompasses this more specific trait.',
                               'mapping': 'broader', 'term': trait},
        'note': f'{trait} -> TO:0000387 plant trait. A broader parent class does not identify or merge traits.'})
    closed_predicates = verify_closed_predicates(ref)
    closed_predicate_list = [
        {
            'alias': alias,
            'id': meta['id'],
            'iri': meta['iri'],
            'display_label': meta['display_label'],
            'upstream_label': meta['upstream_label'],
            'effective_domain': meta['effective_domain'],
            'effective_range': meta['effective_range'],
            'directed': meta['directed'],
            'admission_rule': meta['admission_rule'],
            'definition': meta['definition'],
        }
        for alias, meta in closed_predicates.items()
    ]
    closed_atomic_extraction = {
        'title': 'Atomic extraction under an approved relation',
        'source': (
            'Inoculation with Agent A increased Organ O Trait Q by 15% and Organ P Trait Q by 8% '
            'in Group G compared with Group H under Condition C.'
        ),
        'output': {'claims': [
            {
                'subject': 'Inoculation with Agent A', 'predicate_alias': 'P1',
                'object': 'Organ O Trait Q',
                'qualifiers': {'population': 'Group G', 'comparator': 'Group H', 'condition': 'under Condition C', 'metric': '15%'},
                'subject_anchor': 'Inoculation with Agent A', 'relation_anchor': 'increased',
                'object_anchor': 'Organ O Trait Q by 15% and Organ P Trait Q', 'evidence_sentence_ids': [1],
            },
            {
                'subject': 'Inoculation with Agent A', 'predicate_alias': 'P1',
                'object': 'Organ P Trait Q',
                'qualifiers': {'population': 'Group G', 'comparator': 'Group H', 'condition': 'under Condition C', 'metric': '8%'},
                'subject_anchor': 'Inoculation with Agent A', 'relation_anchor': 'increased',
                'object_anchor': 'Organ O Trait Q by 15% and Organ P Trait Q', 'evidence_sentence_ids': [1],
            },
        ]},
        'explanation': (
            'Coordinated outcomes with distinct measurements split into separate atomic claims under the approved P1 '
            'relation while keeping shared experimental conditions, population, and comparator in qualifiers.'
        ),
    }
    closed_comparison_abstention = {
        'title': 'Comparison without an approved property',
        'source': 'Group A had 12% lower Trait Q than Group B.',
        'output': {'claims': []},
        'explanation': 'The passage reports a difference between groups. An approved assertion requires a source-stated relationship that satisfies its definition.',
    }
    has_part = next(k for k, iri in relation_iris.items() if iri.endswith('/BFO_0000051'))
    closed_source = 'A root system consists of a root.'
    closed_claim = {
        'subject': 'root system', 'object': 'root',
        'predicate_alias': 'P7', 'relation_anchor': 'consists of',
        'qualifiers': {}, 'subject_anchor': 'root system', 'object_anchor': 'root',
        'evidence_sentence_ids': [1],
    }
    closed_extraction = {
        'stageName': 'Stage 1 · Bounded Claim Extraction',
        'description': 'Extracts direct, atomic scientific claims under one approved RO/BFO relation (P1–P12), preserving verbatim source wording and experimental qualifiers.',
        'summary': {
            'input': 'Parsed PDF text packets split into bounded reading-context windows anchored by page and sentence IDs.',
            'method': 'An LLM extracts direct scientific assertions restricted strictly to 12 approved RO/BFO predicates (P1 to P12) and attaches literal text spans plus up to 5 study qualifiers (crop/species, population, condition, comparator, metric).',
            'guardrails': 'Rejects unanchored claims, multi-trait bundles, vague verbs outside P1–P12, and uncited qualifiers; flags review holds without fabricating missing endpoints.',
            'output': 'Evidence-linked atomic candidate claims paired with source quotes, citations, and fixed predicate choices.',
        },
        'systemPrompt': build_extraction_prompt(0, 'target_context', closed_relations=True, reference=ref),
        'input': 'Target sentence IDs: [1]\nSupport-only sentence IDs (never extract separate findings): []\n'
                 'Allowed evidence sentence IDs: [1]\n<extraction_target>\n[1] [page 1] ' + closed_source + '\n</extraction_target>',
        'output': {'claims': [closed_claim]},
    }
    has_part_meta = relations[has_part]
    entity_grounding = {
        'stageName': 'Stage 2 · Entity Grounding',
        'description': 'Grounds the subject and object of each extracted claim to established OBO Foundry classes using hybrid dense + lexical retrieval (RRF), keeping the Stage 1 relation fixed.',
        'summary': {
            'input': 'Extracted claims with fixed Stage 1 predicates, source quotes, and paper-defined abbreviations.',
            'method': 'Runs local hybrid candidate retrieval (lexical, BM25, BGE embeddings, ChEBI formulas, plus dietary nutrient lookup for bare food phosphorus) with zero LLM calls, then prompts an LLM to align subject and object to the top 14 OBO Foundry classes.',
            'guardrails': 'Checks BFO domain/range type compatibility against the fixed relation, blocks elemental atomic collapse for non-atomic food mentions, and separates exact matches from broader classifications.',
            'output': 'Clean ontology-grounded claims indexed across shared entity and relation registries.',
        },
        'systemPrompt': ENTITY_SYSTEM,
        'input': {
            'scope': {k: ref.profile[k] for k in ('id', 'crop', 'research_question', 'scope_description')},
            'entity_only': True,
            'terms': {system: terms[system], root: terms[root]},
            'relations': {has_part: has_part_meta},
            'reading_contexts': {},
            'claims': {'C1': {
                'subject': 'root system', 'relation': 'has part', 'object': 'root',
                'fixed_property': {
                    'alias': 'P7', 'iri': 'http://purl.obolibrary.org/obo/BFO_0000051', 'id': 'BFO:0000051',
                    'label': has_part_meta['label'], 'proposed_mapping': 'exact',
                },
                'source_evidence_span': closed_source,
                'subject_candidates': [system], 'object_candidates': [root],
                'domain_compatible_subject_candidates': [system],
                'range_compatible_object_candidates': [root],
                'verified_relation_endpoint_matches': {'subject': [system], 'object': [root]},
            }},
        },
        'output': {'results': [{
            'claim_id': 'C1', 'scope': 'in_scope',
            'scope_reason': 'Plant anatomical composition fits the selected crop scope.',
            'subject_denotation': 'concept', 'object_denotation': 'concept',
            'subject': {'reason': 'The generic structure matches the supplied root system concept.', 'mapping': 'exact', 'term': system},
            'object': {'reason': 'The generic structure matches the supplied root concept.', 'mapping': 'exact', 'term': root},
        }]},
        'aliasExplanation': f'{system} -> PO:0025025 root system; {root} -> PO:0009005 root; fixed P7 -> BFO:0000051 has part.',
    }
    cross_paper_merging = {
        'stageName': 'Stage 3 · Cross-paper merging',
        'description': (
            'Deterministically links grounded eligible claims across papers using separate directional flow (↓) '
            'and ontology class alignment (↔) arrows without LLM calls or unstated biological assertions.'
        ),
        'summary': {
            'input': 'All in-scope, classification-eligible assertions across all grounded papers in the corpus.',
            'method': 'Deterministically builds multi-hop chains (zero LLM calls) by chaining directional claim edges (↓), inverting dietary intake has-input edges forward as input-of (P8⁻¹), and joining shared or hierarchical OBO classes (↔).',
            'guardrails': 'Permits crop-to-human bridging only through food and dietary nutrient classes (FOODON, CDNO, dietary phosphate), blocks non-food plant-to-animal process joins, and preserves differing study conditions as context-shift badges.',
            'output': 'Verified cross-paper Crop-to-Human pathways, shared multi-paper concept hubs, blocked kingdom pairs, and vertical node-and-arrow diagrams.',
        },
        'rules': [
            'Two-arrow separation: directional extracted predicates stay ↓ flow edges, while same-class, subclass, and superclass joins render as ↔ ontology connectors.',
            'Upstream dietary intake inversion (P8⁻¹): claims extracted under P8 (has input, RO:0002233) also traverse forward as P8⁻¹ (input of, RO:0002352) from food/nutrient to intake or exposure.',
            'Food & nutrient bridge with kingdom guard: FOODON and CDNO food/nutrient classes (such as FOODON:03315354 potato and CDNO:0000018 dietary phosphorus) bridge crop/cooking claims into human intake, while non-food plant/animal processes (such as GO:0006793) remain blocked across kingdoms.',
            'Three-tier context compatibility: joins with matching crop, population, condition, and comparator stay compatible; differing study contexts stay explicitly badged as context_shift.',
        ],
        'workedExample': {
            'title': 'Worked Crop → Human journey (0 LLM calls)',
            'steps': [
                'Lisciani 2025 (plant): FOODON:03315354 (potato) ↓ P7 has part → CDNO:0000018 (dietary phosphorus)',
                'Bridge (↔ subclass): CDNO:0000018 (dietary phosphorus) ↔ CDNO:0000011 (macro element)',
                'Lampen 2026 (human): CDNO:0000011 ↓ P7 has part → CHEBI:24838 (inorganic phosphate)',
                'Calvo 2023 (human): CHEBI:24838 ↓ P8⁻¹ input of (RO:0002352) → ECTO:9001345 (exposure to phosphate)',
                'Calvo 2023 (human): ECTO:9001345 ↓ P10 causes or contributes to condition → MONDO:0005300 (chronic kidney disease)',
            ],
            'blockedNote': 'Meanwhile, joining plant GO:0006793 (phosphorus metabolic process) in Chea 2024 to animal GO:0006793 in Lampen 2026 is recorded as blocked_cross_domain.',
        },
    }
    return {'source': source, 'extractionExample': extraction, 'groundingExample': grounding,
            'closedExtractionExample': closed_extraction, 'entityGroundingExample': entity_grounding,
            'crossPaperMergingExample': cross_paper_merging,
            'closedPredicateList': closed_predicate_list,
            'closedAtomicExtractionExample': closed_atomic_extraction,
            'closedComparisonAbstentionExample': closed_comparison_abstention,
            'atomicExtractionExample': atomic, 'roleExamples': roles, 'alignmentExamples': alignment_examples}


def render_examples():
    return '// Generated offline from active prompts and established vocabulary. Do not hand-edit.\n' + '\n'.join(
        f'export const {key} = {json.dumps(value, ensure_ascii=False, indent=2)}\n' for key, value in examples().items())


if __name__ == '__main__':
    target = Path(__file__).resolve().parents[2] / 'frontend/src/ontology/appendixExamples.js'
    target.write_text(render_examples(), encoding='utf-8')
