"""Production JSON and TSV paths with fixed responses and the release-pinned local index."""
import json
from types import SimpleNamespace

import pytest

from backend.domain.classification import classification_counts
from backend.ontology.catalog import catalog
from backend.ontology.inventory import open_inventory
from backend.stages.claim_extraction.extractor import _proposed_candidate
from backend.stages.ontology_grounding.grounder import ground_claims
from backend.stages.ontology_grounding.shortlists import cultivar_definitions


def test_species_evidence_is_local_and_admits_only_a_broader_taxon():
    ref = catalog()
    claim = {'subject': 'Example Alpha', 'object': 'yield', 'candidate_id': 'a', 'source_document_id': 'one',
             'source_pdf_sha256': 'sha-one', 'source_evidence_span': 'The potato cultivars Example Alpha and Example Beta differed.'}
    other = {**claim, 'candidate_id': 'b', 'source_document_id': 'two', 'source_pdf_sha256': 'sha-two',
             'source_evidence_span': 'Example Alpha was measured.'}
    context = cultivar_definitions([claim, other], ref)
    assert set(context) == {('a', 'subject')}
    species = ref.profile['crop_iri']
    decision = {'iri': species, 'mapping': 'exact'}
    open_inventory().admit({**claim, 'subject_cultivar_source': context['a', 'subject']},
                           'subject', decision, ref.terms[species])
    assert decision['mapping'] == 'broader'


def test_extended_property_requires_own_claim_and_compatible_types():
    from backend.providers.structured_output import SchemaValidationError
    from backend.stages.ontology_grounding.grounder import request_packet
    from backend.stages.ontology_grounding.rows import parse_rows

    ref, inventory = catalog(), open_inventory()
    claim = {'candidate_id': 'a', 'subject': 'root', 'relation': 'decreased', 'object': 'root length'}
    other = {**claim, 'candidate_id': 'b', 'relation': 'part of'}
    packet, _ = request_packet([claim, other], ref, inventory=inventory)
    alias = next(k for k, v in packet['relations'].items() if v['label'] == 'negatively regulates characteristic')
    assert alias in packet['claims']['C1']['relation_candidates']
    assert alias not in packet['claims']['C2']['relation_candidates']
    assert len(packet['relations']) <= 32
    lines = [f'{key}.{role}\t{kind}\t{decision}\t{target}\tTest evidence'
             for key in packet['claims'] for role, kind, decision, target in (
                 ('scope', 'scope', 'in_scope', '-'), ('subject', 'concept', 'unresolved', '-'),
                 ('object', 'concept', 'unresolved', '-'), ('relation', 'relation', 'broader', alias))]
    with pytest.raises(SchemaValidationError, match='C2.relation'):
        parse_rows('\n'.join(lines), packet)
    root = 'http://purl.obolibrary.org/obo/PO_0009005'
    bindings = {r: {'iri': root, 'mapping': 'exact'} for r in ('subject', 'object')}
    issue = inventory.relation_issue(ref, claim, bindings, 'http://purl.obolibrary.org/obo/RO_0019002', 'exact')
    assert 'compatible endpoint types' in issue


def test_treatment_retrieval_requires_its_own_document_label():
    from backend.domain.treatment_labels import document_treatment_labels
    from backend.stages.ontology_grounding.shortlists import treatment_candidates
    ref = catalog()
    sentence = 'Phosphorus levels supplied in nutrient solution were labelled as T1 and T2.'
    labels = document_treatment_labels([{'packet_number': 1, 'sentences': [{'page': 1, 'text': sentence}]}],
                                      'paper', 'hash')
    claim = {'subject': 'T1', 'source_document_id': 'paper', 'source_pdf_sha256': 'hash', 'source_treatment_labels': labels}
    iris, setup = treatment_candidates(ref, claim, 'subject', [])
    assert setup['text'] == sentence and any(ref.terms[i]['label'] == 'phosphorus nutrient exposure' for i in iris)
    assert treatment_candidates(ref, {**claim, 'source_pdf_sha256': 'other'}, 'subject', []) == ([], None)
    assert treatment_candidates(ref, {**claim, 'subject': 'T3'}, 'subject', []) == ([], None)
    inventory = open_inventory()
    term = next(ref.terms[i] for i in iris if ref.terms[i]['label'] == 'phosphorus nutrient exposure')
    decision = {'iri': term['iri'], 'mapping': 'exact'}
    without_definition = decision.copy()
    inventory.admit(claim, 'subject', without_definition, term)
    assert without_definition['mapping'] == 'unresolved'
    inventory.admit({**claim, 'subject_treatment_source': setup}, 'subject', decision, term)
    assert decision['mapping'] == 'broader' and decision['iri'] == term['iri']
    measured_only = {'iri': term['iri'], 'mapping': 'broader'}
    measured_setup = {'text': 'Existing soil phosphorus levels were measured and labelled as T1 and T2.'}
    inventory.admit({**claim, 'subject_treatment_source': measured_setup}, 'subject', measured_only, term)
    assert measured_only['mapping'] == 'unresolved'


def test_shortlist_excludes_incompatible_quantity_and_keeps_a_broader_class():
    ref, inventory = catalog(), open_inventory()
    terms = inventory.candidates(ref, 'phosphorus concentration in roots', '', '', [])
    iris = {term['iri'].rsplit('/', 1)[-1] for term in terms}
    assert 'TO_0001124' not in iris
    assert 'TO_0006045' in iris and 'PATO_0000033' in iris
    terms = inventory.candidates(ref, 'total root length', '', '', [])
    iris = {term['iri'].rsplit('/', 1)[-1] for term in terms}
    assert 'TO_0000227' not in iris and 'TO_0000985' in iris


def test_parallel_trend_withholds_causal_property():
    ref, inventory = catalog(), open_inventory()
    process = 'http://purl.obolibrary.org/obo/PECO_0007397'
    trait = 'http://purl.obolibrary.org/obo/PATO_0001025'
    relation = 'http://purl.obolibrary.org/obo/RO_0019002'
    claim = {'subject': 'nutrient supplementation', 'object': 'growth efficiency', 'relation': 'decreased',
             'source_evidence_span': 'As nutrient supplementation increased, growth efficiency declined.'}
    bindings = {'subject': {'iri': process, 'mapping': 'broader'}, 'object': {'iri': trait, 'mapping': 'broader'}}
    assert 'parallel changes' in inventory.relation_issue(ref, claim, bindings, relation, 'exact')
    explicit = {**claim, 'source_evidence_span': 'Nutrient supplementation decreased growth efficiency.'}
    assert 'parallel changes' not in (inventory.relation_issue(ref, explicit, bindings, relation, 'exact') or '')


@pytest.mark.parametrize('wire', ['json', 'tsv'])
def test_ground_comparison_outside_core_with_checkpoint(tmp_path, wire):
    try:
        inventory = open_inventory()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    reference = catalog()
    proposal = {'subject': 'Group A', 'relation': 'had higher', 'object': 'shoot system phosphorus content',
                'qualifiers': {'comparator': 'Group B', 'metric': '12%'},
                'measured_comparison': {'comparison_axis': 'group', 'direction': 'higher',
                                        'statistical_significance': 'not_reported', 'comparator_quote': 'Group B'}}
    candidate = {**_proposed_candidate(proposal), 'candidate_id': 'comparison-1',
                 'source_evidence_span': 'Group A had 12% higher shoot system phosphorus content than Group B.'}
    calls = []

    class Client:
        settings = SimpleNamespace(ontology_grounding_output_format=wire)

        def select(self, user):
            packet = json.loads(user)
            calls.append(packet)
            return next(a for a in packet['claims']['C1']['object_candidates']
                        if packet['terms'][a]['iri'].endswith('TO_0020114'))

        def chat_json(self, system, user, **kwargs):
            return {'results': [{'claim_id': 'C1', 'scope': 'in_scope', 'scope_reason': 'Study comparison',
                                 'subject_denotation': 'particular', 'object_denotation': 'concept',
                                 'subject': {'term': None, 'mapping': 'unresolved', 'reason': 'Study group'},
                                 'relation': {'term': None, 'mapping': 'unresolved', 'reason': 'Comparison'},
                                 'object': {'term': self.select(user), 'mapping': 'exact', 'reason': 'Trait label'}}]}

        def chat_text(self, system, user, **kwargs):
            term = self.select(user)
            return '\n'.join(['C1.scope\tscope\tin_scope\t-\tStudy comparison',
                              'C1.subject\tparticular\tunresolved\t-\tStudy group',
                              f'C1.object\tconcept\texact\t{term}\tTrait label',
                              'C1.relation\trelation\tunresolved\t-\tComparison'])

    def run():
        return ground_claims([candidate], tmp_path, reference, Client(), inventory=inventory)

    rows = run()
    binding = rows[0]['bindings']['object']
    assert binding['iri'] not in reference.profile['allowed_classes']
    assert binding['retrieved_term']['source']
    assert rows[0]['comparison_assessment']['complete']
    counts = classification_counts(rows, reference)
    assert counts['complete_measured_comparisons'] == counts['classified_endpoint_mentions'] == 1
    assert counts['classified_claims'] == 0 and not rows[0]['assembly_eligible']
    assert run()[0]['input_signature'] == rows[0]['input_signature']
    assert len(calls) == 1


def test_formula_charge_survives_shortlisting():
    from backend.evaluation.experimental_retrieval import parse_chemical_formula, formula_lookup_candidates
    assert parse_chemical_formula('NO3−') == ('N1O3', '-1')
    assert parse_chemical_formula('HPO₄²⁻') == ('H1O4P1', '-2')
    assert parse_chemical_formula('PO4^3-') == ('O4P1', '-3')
    assert parse_chemical_formula('Fe3+') == ('Fe1', '3')
    neutral = {'iri': 'neutral', 'label': 'neutral candidate'}
    index = {'by_exact': {'N1O3|chg=0': [neutral]}, 'by_signature': {'N1O3': [neutral]}}
    assert formula_lookup_candidates(index, 'NO3−') == []


def test_production_admission_blocks_unaveraged_weight_and_keeps_specific_rates_partial():
    try:
        inventory = open_inventory()
    except RuntimeError as exc:
        pytest.skip(str(exc))
    for label, endpoint, curie, expected in (
        ('root dry weight', 'root dry weight', 'TO_0000078', 'unresolved'),
        ('rate', 'nutrient translocation rate', 'PATO_0000161', 'broader'),
        ('composition', 'soil nitrate content', 'PATO_0000025', 'broader'),
    ):
        iri = 'http://purl.obolibrary.org/obo/' + curie
        decision = {'mapping': 'exact', 'label': label, 'iri': iri}
        inventory.admit({'object': endpoint, 'source_evidence_span': endpoint}, 'object', decision,
                        {'iri': iri, 'label': label})
        assert decision['mapping'] == expected
        if expected == 'broader':
            assert decision['components']['grounding_completeness'] == 'partial'


def test_option1_fixed_property_relation_issue_skips_legacy_text_gates():
    ref, inventory = catalog(), open_inventory()
    p5_claim = {
        'subject': 'root surface area',
        'relation': 'correlated with',
        'object': 'total phosphorus uptake',
        'proposed_relation_iri': 'http://purl.obolibrary.org/obo/RO_0002610',
        'qualifiers': {'population': 'Milva'},
        'source_evidence_span': (
            'Total root length and root surface area had a significant correlation with '
            'total P uptake in Milva, but no such relationships were found in Lady Claire.'
        ),
    }
    p5_bindings = {
        'subject': {'iri': 'http://purl.obolibrary.org/obo/PATO_0001323', 'mapping': 'exact'},
        'object': {'iri': 'http://purl.obolibrary.org/obo/TO_0000511', 'mapping': 'broader'},
    }
    assert inventory.relation_issue(ref, p5_claim, p5_bindings, 'http://purl.obolibrary.org/obo/RO_0002610', 'exact') is None

    # Broader/broader sharing the same generic umbrella class stays permitted.
    shared_broader_bindings = {
        'subject': {'iri': 'http://purl.obolibrary.org/obo/TO_0000511', 'mapping': 'broader'},
        'object': {'iri': 'http://purl.obolibrary.org/obo/TO_0000511', 'mapping': 'broader'},
    }
    assert inventory.relation_issue(ref, p5_claim, shared_broader_bindings, 'http://purl.obolibrary.org/obo/RO_0002610', 'exact') is None

    # Exact-exact self-link, unresolved endpoints, passive reversal failures, and type conflicts stay blocked.
    exact_self = {r: {'iri': 'http://purl.obolibrary.org/obo/TO_0000511', 'mapping': 'exact'} for r in ('subject', 'object')}
    assert 'self-link' in inventory.relation_issue(ref, p5_claim, exact_self, 'http://purl.obolibrary.org/obo/RO_0002610', 'exact').lower()
    unresolved_obj = {**p5_bindings, 'object': {'iri': None, 'mapping': 'unresolved'}}
    assert 'resolved' in inventory.relation_issue(ref, p5_claim, unresolved_obj, 'http://purl.obolibrary.org/obo/RO_0002610', 'exact').lower()
    assert 'passive' in inventory.relation_issue(ref, {**p5_claim, 'passive_role_reversal_verified': False}, p5_bindings, 'http://purl.obolibrary.org/obo/RO_0002610', 'exact').lower()

    root_iri = 'http://purl.obolibrary.org/obo/PO_0009005'
    p1_conflict = {
        'subject': 'root', 'relation': 'increased', 'object': 'root surface area',
        'proposed_relation_iri': 'http://purl.obolibrary.org/obo/RO_0019001',
        'source_evidence_span': 'Root increased root surface area.',
    }
    conflict_bindings = {'subject': {'iri': root_iri, 'mapping': 'exact'}, 'object': p5_bindings['subject']}
    assert 'compatible endpoint types' in inventory.relation_issue(
        ref, p1_conflict, conflict_bindings, 'http://purl.obolibrary.org/obo/RO_0019001', 'exact'
    )


def test_shortlists_work_without_212_curated_classes():
    from backend.stages.ontology_grounding.shortlists import cultivar_definitions, treatment_candidates
    from backend.domain.treatment_labels import document_treatment_labels

    class NoCuratedCatalog:
        def __init__(self, orig):
            self.terms = orig.terms
            self.graph = orig.graph
            self.profile = {**orig.profile, 'allowed_classes': []}

    ref, inventory = catalog(), open_inventory()
    no_curated = NoCuratedCatalog(ref)

    for phrase, expected_id, req in [
        ('phosphorus concentration in roots', 'PATO_0000033', ()),
        ('total root length', 'TO_0000985', ()),
        ('limited phosphorus supply', 'PECO_0001046', ()),
        ('Phosphorus deficiency', 'PECO_0001046', ('http://purl.obolibrary.org/obo/BFO_0000015',)),
        ('Phosphorus scarcity', 'PECO_0001046', ('http://purl.obolibrary.org/obo/BFO_0000015',)),
        ('indole-3-acetic acid concentration', 'PATO_0000033', ('http://purl.obolibrary.org/obo/BFO_0000020',)),
        ('co-inoculation of potato with plant growth-promoting rhizobacteria', 'PECO_0007246', ()),
    ]:
        terms = inventory.candidates(no_curated, phrase, '', phrase, [], required_ancestors=req)
        ids = {t['iri'].rsplit('/', 1)[-1] for t in terms}
        assert expected_id in ids, f'Missing {expected_id} for {phrase}; got {sorted(ids)}'

    sentence = 'Phosphorus levels supplied in nutrient solution were labelled as T1 and T2.'
    labels = document_treatment_labels([{'packet_number': 1, 'sentences': [{'page': 1, 'text': sentence}]}], 'paper', 'hash')
    claim = {'subject': 'T1', 'source_document_id': 'paper', 'source_pdf_sha256': 'hash', 'source_treatment_labels': labels}
    iris, setup = treatment_candidates(no_curated, claim, 'subject', [], inventory=inventory)
    assert setup and 'http://purl.obolibrary.org/obo/PECO_0007397' in iris

    cultivar_claim = {'candidate_id': 'c1', 'subject': 'Example Alpha', 'object': 'yield',
                      'source_document_id': 'one', 'source_pdf_sha256': 'sha',
                      'source_evidence_span': 'Potato cultivars Example Alpha and Example Beta were grown.'}
    assert ('c1', 'subject') in cultivar_definitions([cultivar_claim], no_curated, inventory=inventory)


def test_ontology_browser_serves_all_searchable_classes(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from threading import RLock
    from backend.shared.ontology_browser import create_router

    workspace = SimpleNamespace(ONTOLOGY_DIR=tmp_path, _lock=RLock(), _jobs={},
                                _pdf_path=lambda _: 'paper.pdf', _output_dir=lambda _: tmp_path)
    app = FastAPI()
    app.include_router(create_router(workspace, read_only=True, snapshot_loader=lambda _p, d: d))
    with TestClient(app) as client:
        status = client.get('/api/ontology/status').json()
        assert status['term_counts']['class'] == 945758
        assert status['class_source'] == 'searchable_inventory'

        found = client.get('/api/ontology/terms?kind=class&q=shoot+system+phosphorus+content').json()
        assert any(t['id'] == 'TO:0020114' for t in found['terms'])

        curie_found = client.get('/api/ontology/terms?kind=class&q=PATO:0000033').json()
        assert curie_found['terms'][0]['label'] == 'concentration of'

        detail = client.get('/api/ontology/term?iri=http://purl.obolibrary.org/obo/TO_0020114').json()
        assert detail['label'] == 'shoot system phosphorus content'
        assert detail['lineage_paths'] and detail['lineage_paths'][0][-1]['iri'] == 'http://purl.obolibrary.org/obo/TO_0020114'

        browse = client.get('/api/ontology/terms?kind=class&limit=5').json()
        assert browse['total'] == 945758
        assert browse['terms'][0]['id'].startswith('BFO:')
        assert all(t['definition'] for t in browse['terms'])

        hgnc = client.get('/api/ontology/terms?kind=class&q=HGNC:11019').json()
        assert hgnc['terms'][0]['id'] == 'HGNC:11019'
        assert hgnc['terms'][0]['label'] == 'SLC34A1'


def test_rrf_fuse_combines_channels_and_preserves_order():
    from backend.ontology.semantic_index import EXCLUDED_DENSE_MULTIPLIER, rrf_fuse

    fused = rrf_fuse([
        ('lexical', 1.0, [{'iri': 'A'}, {'iri': 'B'}]),
        ('fts5', 1.0, [{'iri': 'B'}, {'iri': 'C'}]),
        ('dense_bge', 1.2, [{'iri': 'B'}, {'iri': 'D'}]),
    ], k=60)
    ranked = sorted(fused.values(), key=lambda x: -x['rrf_score'])
    assert ranked[0]['iri'] == 'B'
    assert set(ranked[0]['ranks']) == {'lexical', 'fts5', 'dense_bge'}

    scaled = rrf_fuse(
        [('lexical', 1.0, [{'iri': 'X', 'class_source': 'chebi.owl.gz'}])],
        k=35,
        source_multipliers={'chebi.owl.gz': EXCLUDED_DENSE_MULTIPLIER},
    )
    assert scaled['X']['rrf_score'] == pytest.approx((1.0 / 36) * EXCLUDED_DENSE_MULTIPLIER)


def test_hybrid_rrf_retrieves_semantic_paraphrases():
    from backend.ontology.semantic_index import open_semantic_index

    idx = open_semantic_index('analysis/single_stage/v4_pilot')
    assert idx is not None
    hits = idx.search('root biomass', limit=10, min_score=0.45)
    assert any(h['iri'].endswith(('CO_330:0000702', 'TO_0000457', 'TO_0001019')) for h in hits)

    ref, inv = catalog(), open_inventory()
    for phrase, expected_ids in [
        ('root biomass', {'CO_330:0000702', 'TO_0000457', 'TO_0000279'}),
        ('indole-3-acetic acid concentration', {'PATO_0000033', 'CDNO_0200422'}),
    ]:
        cands = inv.candidates(ref, phrase, '', phrase, [])
        got_ids = {c['iri'].rsplit('/', 1)[-1] for c in cands}
        assert got_ids & expected_ids, f'Missing any of {expected_ids} for {phrase!r}; got {got_ids}'


def test_clean_production_admission_and_upper_lineage_bridges():
    ref, inv = catalog(), open_inventory()

    # 1. Ontology-root upper-type bridges in Inventory.lineage (TO, CO_330 traits, PECO), leaving GO MF unbridged.
    assert 'http://purl.obolibrary.org/obo/PATO_0000001' in inv.lineage(ref, 'http://purl.obolibrary.org/obo/TO_0000654')
    assert 'http://purl.obolibrary.org/obo/PATO_0000001' in inv.lineage(ref, 'https://cropontology.org/rdf/CO_330:0000702')
    assert 'http://purl.obolibrary.org/obo/PATO_0000001' not in inv.lineage(ref, 'https://cropontology.org/rdf/CO_330:0000181')
    assert 'http://purl.obolibrary.org/obo/BFO_0000015' in inv.lineage(ref, 'http://purl.obolibrary.org/obo/PECO_0007067')
    mf_lineage = set(inv.lineage(ref, 'http://purl.obolibrary.org/obo/GO_0003993'))
    assert 'http://purl.obolibrary.org/obo/BFO_0000002' not in mf_lineage
    assert 'http://purl.obolibrary.org/obo/PATO_0000001' not in mf_lineage

    # 2. PECO:0001046 admits Phosphorus scarcity/deficiency without legacy literal phosphate-form gate,
    #    while high phosphorus stays blocked; bare deficiency still cannot map to PECO:0007397.
    peco_lim = inv.get_term('http://purl.obolibrary.org/obo/PECO_0001046')
    peco_sup = inv.get_term('http://purl.obolibrary.org/obo/PECO_0007397')
    for phrase, expected in [('Phosphorus scarcity', 'broader'), ('Phosphorus deficiency', 'broader'), ('high phosphorus treatment', 'unresolved')]:
        dec = {'iri': peco_lim['iri'], 'label': peco_lim['label'], 'mapping': 'broader'}
        inv.admit({'subject': phrase, 'source_evidence_span': f'{phrase} reduced root growth.'}, 'subject', dec, peco_lim)
        assert dec['mapping'] == expected

    bare_sup = {'iri': peco_sup['iri'], 'label': peco_sup['label'], 'mapping': 'broader'}
    inv.admit({'subject': 'Phosphorus deficiency', 'source_evidence_span': 'Phosphorus deficiency reduced root growth.'}, 'subject', bare_sup, peco_sup)
    assert bare_sup['mapping'] == 'unresolved'

    multi_lvl = 'exposed to both extremely low (0 mg phosphorus L-1) and high phosphorus levels (>= 30 mg phosphorus L-1)'
    arm_dec = {'iri': peco_sup['iri'], 'label': peco_sup['label'], 'mapping': 'broader'}
    inv.admit({'subject': multi_lvl, 'source_evidence_span': f'Plants {multi_lvl}.'}, 'subject', arm_dec, peco_sup)
    assert arm_dec['mapping'] == 'broader'

    # 3. Efficiency (PATO:0001029) marks grounding_completeness='complete' while keeping shared_concept=False.
    eff_dec = {'iri': 'http://purl.obolibrary.org/obo/PATO_0001029', 'label': 'efficiency', 'mapping': 'broader'}
    inv.admit({'object': 'phosphorus use efficiency', 'source_evidence_span': 'PUE increased.'},
              'object', eff_dec, {'iri': 'http://purl.obolibrary.org/obo/PATO_0001029', 'label': 'efficiency'})
    assert eff_dec['mapping'] == 'broader'
    assert eff_dec['components']['grounding_completeness'] == 'complete'
    assert eff_dec['components']['shared_concept'] is False

    # 4. Parenthetical cultivar declarations in source_reading_context match same-document only.
    c_claim = {'candidate_id': 'c-parent', 'subject': 'Example Alpha', 'object': 'yield',
               'source_document_id': 'doc-a', 'source_pdf_sha256': 'sha-a',
               'source_evidence_span': 'Example Alpha had higher yield.',
               'source_reading_context': '[1] Two potato cultivars (Example Alpha and Example Beta) were tested.'}
    other_doc = {**c_claim, 'candidate_id': 'c-other', 'source_document_id': 'doc-b', 'source_pdf_sha256': 'sha-b',
                 'source_reading_context': '[1] Independent paper without cultivar declaration.'}
    found = cultivar_definitions([c_claim, other_doc], ref, inventory=inv)
    assert ('c-parent', 'subject') in found and ('c-other', 'subject') not in found


def test_ncbitaxon_lineage_and_multihop_bacterial_retrieval():
    from backend.ontology.property_types import (
        BFO_CONTINUANT,
        BFO_INDEPENDENT_CONTINUANT,
        BFO_MATERIAL_ENTITY,
        BFO_PROCESS,
    )

    ref, inv = catalog(), open_inventory()
    bact_lineage = set(inv.lineage(ref, 'http://purl.obolibrary.org/obo/NCBITaxon_2'))
    assert {BFO_MATERIAL_ENTITY, BFO_INDEPENDENT_CONTINUANT, BFO_CONTINUANT} <= bact_lineage
    assert BFO_CONTINUANT not in set(inv.lineage(ref, 'http://purl.obolibrary.org/obo/NCBITaxon_1'))

    for phrase in (
        'plant growth-promoting rhizobacteria-based seed coating',
        'combined plant growth-promoting rhizobacteria (CM1 and CM2)',
        'plant growth-promoting rhizobacteria treatments',
    ):
        ids = {t['iri'].rsplit('/', 1)[-1] for t in inv.candidates(
            ref, phrase, '', phrase, [], required_ancestors=(BFO_PROCESS,),
        )}
        assert 'PECO_0007246' in ids, f'Missing PECO_0007246 for {phrase!r}; got {sorted(ids)}'

    taxon_ids = {t['iri'].rsplit('/', 1)[-1] for t in inv.candidates(
        ref, 'plant growth-promoting rhizobacteria', '', 'PGPR promoted root biomass.', [],
        required_ancestors=(BFO_CONTINUANT,),
    )}
    assert 'NCBITaxon_2' in taxon_ids, f'Missing NCBITaxon_2 for PGPR continuant; got {sorted(taxon_ids)}'


def test_foodon_hp_mondo_coverage_and_bfo_lineages():
    from backend.ontology.property_types import (
        BFO_CONTINUANT,
        BFO_MATERIAL_ENTITY,
        BFO_SDC,
        PATO_CHARACTERISTIC,
    )

    ref, inv = catalog(), open_inventory()
    assert {BFO_MATERIAL_ENTITY, BFO_CONTINUANT} <= set(
        inv.lineage(ref, 'http://purl.obolibrary.org/obo/FOODON_00001015')
    )
    assert {PATO_CHARACTERISTIC, BFO_SDC, BFO_CONTINUANT} <= set(
        inv.lineage(ref, 'http://purl.obolibrary.org/obo/HP_0000121')
    )
    assert {BFO_SDC, BFO_CONTINUANT} <= set(
        inv.lineage(ref, 'http://purl.obolibrary.org/obo/MONDO_0005300')
    )

    for phrase, expected_prefixes in [
        ('plant foods', ('FOODON_',)),
        ('dairy products', ('FOODON_',)),
        ('food products', ('FOODON_',)),
        ('animal-derived foods', ('FOODON_',)),
        ('nephrocalcinosis', ('HP_', 'MONDO_')),
        ('tubulointerstitial nephropathy', ('HP_', 'MONDO_')),
        ('renal fibrosis', ('HP_', 'MONDO_')),
        ('chronic kidney disease', ('MONDO_', 'HP_')),
    ]:
        ids = {t['iri'].rsplit('/', 1)[-1] for t in inv.candidates(ref, phrase, '', phrase, [])}
        assert any(tid.startswith(expected_prefixes) for tid in ids), (
            f'Expected {expected_prefixes} for {phrase!r}; got {sorted(ids)}'
        )


def test_cmo_oba_pr_clinical_shortlists_and_bfo_lineages():
    from backend.ontology.property_types import (
        BFO_CONTINUANT,
        BFO_INDEPENDENT_CONTINUANT,
        BFO_MATERIAL_ENTITY,
        BFO_SDC,
        PATO_CHARACTERISTIC,
    )

    ref, inv = catalog(), open_inventory()
    assert {PATO_CHARACTERISTIC, BFO_SDC, BFO_CONTINUANT} <= set(
        inv.lineage(ref, 'http://purl.obolibrary.org/obo/CMO_0000542')
    )
    assert {PATO_CHARACTERISTIC, BFO_SDC, BFO_CONTINUANT} <= set(
        inv.lineage(ref, 'http://purl.obolibrary.org/obo/OBA_2050164')
    )
    assert {BFO_MATERIAL_ENTITY, BFO_INDEPENDENT_CONTINUANT, BFO_CONTINUANT} <= set(
        inv.lineage(ref, 'http://purl.obolibrary.org/obo/PR_000007493')
    )

    fgf_def = [{'abbreviation': 'FGF23', 'expansion': 'fibroblast growth factor 23', 'document': 'doc'}]
    clinical_cases = [
        ('serum phosphate concentration', 'serum phosphate concentration was measured', [], ('CMO_0000542', 'CMO_0000504')),
        ('urinary phosphate excretion', 'urinary phosphate excretion was measured', [], ('CMO_0002907',)),
        ('circulating FGF23 levels', 'circulating FGF23 levels increased', fgf_def, ('OBA_2050164',)),
        ('fibroblast growth factor-23', 'fibroblast growth factor-23', [], ('PR_000007493',)),
        ('parathyroid hormone level', 'parathyroid hormone level was measured', [], ('OBA_VT0002903', 'CMO_0002961', 'OBA_2043094')),
        ('ionized calcium level', 'ionized calcium level was measured', [], ('CMO_0000377', 'OBA_VT0010499')),
        ('carotid intima-media thickness', 'carotid intima-media thickness', [], ('OBA_2090004',)),
    ]
    for phrase, evidence, defs, expected_ids in clinical_cases:
        cands = inv.candidates(ref, phrase, '', evidence, defs)
        assert len(cands) <= 14
        ids = {c['iri'].rsplit('/', 1)[-1] for c in cands}
        assert any(tid in ids for tid in expected_ids), (
            f'Expected one of {expected_ids} in top-14 for {phrase!r}; got {sorted(ids)}'
        )


def test_bare_phosphorus_retrieves_dietary_phosphorus_without_elemental_context():
    from backend.ontology.property_types import BFO_CONTINUANT

    ref, inv = catalog(), open_inventory()
    cands = inv.candidates(
        ref,
        'phosphorus',
        '',
        'Potatoes contain phosphorus and potassium.',
        [],
        required_ancestors=(BFO_CONTINUANT,),
    )
    ids = [c['iri'].rsplit('/', 1)[-1] for c in cands]
    assert 'CDNO_0000018' in ids

    elem_cands = inv.candidates(
        ref,
        'phosphorus',
        'chemical',
        'Elemental white phosphorus atom properties were studied.',
        [],
        required_ancestors=(BFO_CONTINUANT,),
    )
    elem_ids = [c['iri'].rsplit('/', 1)[-1] for c in elem_cands]
    assert 'CHEBI_28659' in elem_ids


