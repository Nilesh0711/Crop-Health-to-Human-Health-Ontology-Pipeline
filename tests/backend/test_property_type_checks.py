"""Offline tests for ontology domain/range/inverse type checks and v9 extraction guards."""
from copy import deepcopy
from threading import RLock
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest

from backend.domain.classification import (
    CLASSIFICATION_POLICY,
    classification_complete,
    classification_counts,
    relation_accepted,
)
from backend.domain.extraction_review import (
    ELIGIBILITY_POLICY,
    REVIEW_POLICY,
    assertion_eligible,
    can_share,
    extraction_warnings,
    mapping_meaning_warnings,
    refresh_eligibility,
)
from backend.ontology.catalog import Catalog, RESOURCE_DIR
from backend.ontology.property_types import (
    BFO_2020_CORE_SOURCE,
    PROPERTY_TYPE_POLICY,
    check_property_types,
    effective_property_restrictions,
    verifiable_upper_types,
)
from backend.ontology.registry import entity_registry
from backend.shared.artifacts import save_json
from backend.shared.ontology_browser import create_router
from backend.stages.ontology_grounding.grounder import catalog_term_map, ground_claims
from tests.backend.test_ontology_grounding import Grounder, claim

BASE = 'http://purl.obolibrary.org/obo/'


@pytest.fixture(scope='module')
def reference():
    return Catalog(RESOURCE_DIR)


def _row(cid='c1', *, subj_iri, obj_iri, rel_iri, rel_mapping='broader', subj_mapping='broader', obj_mapping='broader', warns=None):
    return {
        'candidate_id': cid,
        'claim_signature': f'sig-{cid}',
        'scope': 'in_scope',
        'extraction_warnings': warns or [],
        'bindings': {
            'subject': {'iri': subj_iri, 'label': subj_iri.rsplit('/', 1)[-1] if subj_iri else None, 'mapping': subj_mapping, 'canonical_iri': subj_iri, 'denotation': 'concept', 'occurrence_id': f'occ-s-{cid}', 'reason': 'Kept subject.'},
            'object': {'iri': obj_iri, 'label': obj_iri.rsplit('/', 1)[-1] if obj_iri else None, 'mapping': obj_mapping, 'canonical_iri': obj_iri, 'denotation': 'concept', 'occurrence_id': f'occ-o-{cid}', 'reason': 'Kept object.'},
            'relation': {'iri': rel_iri, 'mapping': rel_mapping, 'reason': 'Model relation reason.'},
        },
        'original_claim': {'subject': 'A', 'relation': 'affected', 'object': 'B', 'qualifiers': {}},
    }


def test_effective_restrictions_and_verifiable_upper_types(reference):
    _, terms, term_iris, _, _ = catalog_term_map(reference)
    peco_alias = next(k for k, v in term_iris.items() if v == BASE + 'PECO_0007246')
    assert terms[peco_alias]['upper_types'] == ['occurrent', 'process']
    assert verifiable_upper_types(reference, BASE + 'PO_0009005') == ['continuant', 'independent continuant', 'material entity']
    assert verifiable_upper_types(reference, BASE + 'GO_0040007') == ['biological_process', 'occurrent', 'process']
    assert verifiable_upper_types(reference, BASE + 'PATO_0000033') == ['characteristic', 'continuant', 'specifically dependent continuant']
    assert verifiable_upper_types(reference, BASE + 'TO_0000511') == ['characteristic', 'continuant', 'specifically dependent continuant']
    assert verifiable_upper_types(reference, BASE + 'GO_0003993') == []

    # Direct + inherited parent restrictions (RO:0002596 -> RO:0002500 -> RO:0002595)
    cap_reg = effective_property_restrictions(reference, BASE + 'RO_0002596')
    assert BASE + 'BFO_0000040' in cap_reg['domain']
    assert BASE + 'BFO_0000015' in cap_reg['range']

    # Swapped inverse restrictions (RO:0002559 inverseOf RO:0002566)
    inf_by = effective_property_restrictions(reference, BASE + 'RO_0002559')
    assert BASE + 'BFO_0000002' in inf_by['domain']
    assert BASE + 'BFO_0000002' in inf_by['range']


@pytest.mark.parametrize('rel_mapping', ['exact', 'broader'])
def test_confirmed_process_vs_continuant_conflict_holds_relation_and_keeps_endpoints(reference, rel_mapping):
    # PECO_0007246 (bacterial plant exposure -> BFO:0000015 process) violates RO:0002566 (continuant -> continuant).
    row = _row(
        'conflict-1',
        subj_iri=BASE + 'PECO_0007246',
        obj_iri=BASE + 'TO_0000387',
        rel_iri=BASE + 'RO_0002566',
        rel_mapping=rel_mapping,
    )
    refresh_eligibility(row, reference)
    assert row['property_type_check']['status'] == 'incompatible'
    assert row['property_type_check']['policy'] == PROPERTY_TYPE_POLICY
    assert not assertion_eligible(row, reference)
    assert not classification_complete(row, reference)
    # Original endpoint bindings and model reason remain preserved untouched.
    assert row['bindings']['subject']['iri'] == BASE + 'PECO_0007246'
    assert row['bindings']['relation']['reason'] == 'Model relation reason.'

    counts = classification_counts([row], reference)
    assert counts['classified_endpoint_mentions'] == 2
    assert counts['classified_relation_mentions'] == 0
    assert counts['proposed_relation_under_review_mentions'] == 1
    assert counts['property_type_conflict_claims'] == 1
    assert counts['property_type_review_claims'] == 0
    assert counts['classified_claims'] == 0

    # Inverse property (RO:0002559) must also catch occurrent in object position!
    inv_row = _row(
        'conflict-inv',
        subj_iri=BASE + 'PO_0009005',
        obj_iri=BASE + 'PECO_0007397',
        rel_iri=BASE + 'RO_0002559',
        rel_mapping=rel_mapping,
    )
    assert check_property_types(inv_row, reference)['status'] == 'incompatible'


def test_insufficient_type_info_blocks_eligibility_and_flags_review(reference):
    # GO_0003993 (acid phosphatase activity) has molecular_function but no BFO continuant link in fragment.
    row = _row(
        'insuff-1',
        subj_iri=BASE + 'PATO_0000033',
        obj_iri=BASE + 'GO_0003993',
        rel_iri=BASE + 'RO_0002566',
        rel_mapping='exact',
        obj_mapping='exact',
    )
    refresh_eligibility(row, reference)
    assert row['property_type_check']['status'] == 'insufficient_type_info'
    assert not assertion_eligible(row, reference)
    counts = classification_counts([row], reference)
    assert counts['property_type_review_claims'] == 1
    assert counts['property_type_conflict_claims'] == 0
    # Insufficient type info is not a confirmed conflict, so broader/accepted status stays separated from exact eligibility.
    assert counts['classified_relation_mentions'] == 1


def test_extraction_hold_excludes_relation_from_accepted_counts_and_surfaces_proposed_review(reference):
    held = _row(
        'held-1',
        subj_iri=BASE + 'PATO_0000033',
        obj_iri=BASE + 'TO_0000387',
        rel_iri=BASE + 'RO_0002566',
        rel_mapping='broader',
        warns=[{'code': 'possible_role_inversion', 'message': 'Reversed trend'}],
    )
    counts = classification_counts([held], reference)
    assert counts['classified_endpoint_mentions'] == 2
    assert counts['classified_relation_mentions'] == 0
    assert counts['proposed_relation_under_review_mentions'] == 1
    assert counts['classified_claims'] == 0
    assert counts['classification_review_claims'] == 1


def test_v9_measurement_and_comparative_trait_in_predicate_guards_without_false_positive_cultivars():
    assert REVIEW_POLICY == 'separate-extraction-and-mapping-review-v12'
    assert ELIGIBILITY_POLICY == 'grounded-predicate-eligibility-v7'
    assert CLASSIFICATION_POLICY == 'ontology-classification-coverage-v6'
    assert PROPERTY_TYPE_POLICY == 'imported-domain-range-inverse-check-v2'

    quant_claim = {
        'subject': 'sugar concentration',
        'relation': 'saw a',
        'object': '17% reduction',
        'object_source_mention': '17% reduction',
        'qualifiers': {},
        'source_evidence_fragments': [{'sentence_number': 1, 'text': 'Milva at P0.5 saw a 17% reduction in sugar concentration.'}],
    }
    assert any(w['code'] == 'possible_measurement_endpoint' for w in extraction_warnings(quant_claim))

    comp_claim = {
        'subject': 'Milva',
        'relation': 'showcased more significant total root length compared to',
        'object': 'Lady Claire',
        'qualifiers': {},
        'source_evidence_fragments': [{'sentence_number': 2, 'text': 'Milva showcased a more significant total root length compared to Lady Claire.'}],
    }
    assert any(w['code'] == 'possible_comparison_trait_in_predicate' for w in extraction_warnings(comp_claim))

    valid_cultivar_bearer = {
        'subject': 'Milva',
        'relation': 'exhibited',
        'object': 'phosphorus use efficiency',
        'qualifiers': {'population': 'Milva'},
        'source_evidence_fragments': [{'sentence_number': 3, 'text': 'Milva exhibited phosphorus use efficiency under low P levels.'}],
    }
    assert not extraction_warnings(valid_cultivar_bearer)


def test_anonymous_owl_restrictions_are_flagged_unverified_not_compatible(reference):
    # RO_0001025 (located in) declares anonymous BNode OWL restrictions on domain and range.
    reqs = effective_property_restrictions(reference, BASE + 'RO_0001025')
    assert reqs['unsupported_domain'] and reqs['unsupported_range']

    row = _row(
        'anon-1',
        subj_iri=BASE + 'PO_0009005',
        obj_iri=BASE + 'PO_0025025',
        rel_iri=BASE + 'RO_0001025',
        rel_mapping='exact',
        subj_mapping='exact',
        obj_mapping='exact',
    )
    refresh_eligibility(row, reference)
    pt = row['property_type_check']
    assert pt['status'] == 'insufficient_type_info'
    assert pt['has_unsupported_restrictions'] is True
    assert any(i['kind'] == 'unsupported_expression' for i in pt['issues'])
    assert not assertion_eligible(row, reference)

    # Named domain conflict must still override unsupported_expression.
    conflict = _row(
        'anon-conflict',
        subj_iri=BASE + 'PECO_0007246',
        obj_iri=BASE + 'PO_0009005',
        rel_iri=BASE + 'RO_0001025',
        rel_mapping='exact',
    )
    assert check_property_types(conflict, reference)['status'] == 'incompatible'

    # Synthetic property with ONLY anonymous blank-node restriction must not pass as compatible.
    synth = SimpleNamespace(terms={
        'urn:p:anon': {'kind': 'object_property', 'domain': ['N12345'], 'range': [], 'parents': [], 'inverse_of': []},
        'urn:c:a': {'kind': 'class', 'label': 'Thing A', 'parents': []},
        'urn:c:b': {'kind': 'class', 'label': 'Thing B', 'parents': []},
    })
    synth_row = _row('synth-anon', subj_iri='urn:c:a', obj_iri='urn:c:b', rel_iri='urn:p:anon', rel_mapping='exact')
    synth_pt = check_property_types(synth_row, synth)
    assert synth_pt['status'] == 'insufficient_type_info'
    assert synth_pt['has_unsupported_restrictions'] is True


def test_ancestor_declared_inverse_restrictions_and_bfo_axiom_citations(reference):
    # Real catalog: RO_0002233 (has input) -> subPropertyOf RO_0000057 (has participant) -> inverseOf RO_0000056 (participates in: domain BFO:0000002, range BFO:0000003).
    has_input = effective_property_restrictions(reference, BASE + 'RO_0002233')
    assert BASE + 'BFO_0000003' in has_input['domain']
    assert BASE + 'BFO_0000002' in has_input['range']

    bad_input = _row(
        'anc-inv-1',
        subj_iri=BASE + 'PO_0009005',  # continuant in process domain
        obj_iri=BASE + 'PO_0009005',
        rel_iri=BASE + 'RO_0002233',
        rel_mapping='exact',
    )
    pt = check_property_types(bad_input, reference)
    assert pt['status'] == 'incompatible'
    conflict_issue = next(i for i in pt['issues'] if i['kind'] == 'incompatible')
    assert conflict_issue['axiom_source']['axiom'] == 'BFO:0000002 owl:disjointWith BFO:0000003'
    assert conflict_issue['axiom_source']['source'] == BFO_2020_CORE_SOURCE

    # Synthetic multi-hop path: P subPropertyOf Q, Q inverseOf R.
    synth = SimpleNamespace(terms={
        'urn:p': {'kind': 'object_property', 'parents': ['urn:q'], 'domain': [], 'range': [], 'inverse_of': []},
        'urn:q': {'kind': 'object_property', 'parents': [], 'domain': [], 'range': [], 'inverse_of': ['urn:r']},
        'urn:r': {'kind': 'object_property', 'parents': [], 'domain': [BASE + 'BFO_0000002'], 'range': [BASE + 'BFO_0000015'], 'inverse_of': []},
    })
    reqs = effective_property_restrictions(synth, 'urn:p')
    assert reqs['domain'] == [BASE + 'BFO_0000015']
    assert reqs['range'] == [BASE + 'BFO_0000002']


@pytest.mark.parametrize('ep_mapping,ep_iri', [
    ('unresolved', None),
    ('related', BASE + 'PATO_0000033'),
    ('narrower', BASE + 'GO_0048767'),
])
def test_missing_required_endpoint_gets_distinct_status_and_stays_unaccepted(reference, ep_mapping, ep_iri):
    row = _row(
        f'miss-{ep_mapping}',
        subj_iri=ep_iri,
        obj_iri=BASE + 'TO_0000387',
        rel_iri=BASE + 'RO_0002566',
        rel_mapping='broader',
        subj_mapping=ep_mapping,
    )
    refresh_eligibility(row, reference)
    pt = row['property_type_check']
    assert pt['status'] == 'missing_required_endpoint'
    assert pt['issues'][0]['kind'] == 'missing_required_endpoint'
    assert pt['issues'][0]['endpoint_mapping'] == ep_mapping
    assert not relation_accepted(row, reference)
    assert not assertion_eligible(row, reference)
    counts = classification_counts([row], reference)
    assert counts['classified_relation_mentions'] == 0
    assert counts['proposed_relation_under_review_mentions'] == 1
    assert counts['property_type_missing_endpoint_claims'] == 1
    assert counts['property_type_review_claims'] == 0


def test_function_vs_measurement_conflation_blocks_even_with_complete_bfo_chain(reference):
    # Clone reference with GO_0003993 linked to BFO_0000002 (continuant) so property_type_check passes as compatible.
    terms = deepcopy(reference.terms)
    terms[BASE + 'GO_0003993']['parents'] = [BASE + 'BFO_0000002']
    terms[BASE + 'PATO_0000033']['parents'] = [BASE + 'BFO_0000002']
    complete_ref = SimpleNamespace(terms=terms, profile=reference.profile, graph=reference.graph)

    row = _row(
        'acp-measured',
        subj_iri=BASE + 'PATO_0000033',
        obj_iri=BASE + 'GO_0003993',
        rel_iri=BASE + 'RO_0002566',
        rel_mapping='exact',
        subj_mapping='broader',
        obj_mapping='exact',
    )
    row['original_claim'] = {
        'subject': 'concentration of phosphorus in the nutrient solution',
        'relation': 'affected',
        'object': 'acid phosphatase activity',
        'qualifiers': {},
        'source_evidence_span': 'Phosphorus concentration affected acid phosphatase activity in roots.',
    }
    row['document_key'] = 'doc'
    refresh_eligibility(row, complete_ref)
    assert row['property_type_check']['status'] == 'compatible'
    assert any(w['code'] == 'possible_function_vs_measurement_conflation' for w in row['mapping_review_warnings'])
    assert not assertion_eligible(row, complete_ref)
    assert not classification_complete(row, complete_ref)
    assert not can_share(row, 'object')
    assert row['bindings']['object']['entity_id'] == row['bindings']['object']['occurrence_id']
    assert not row['bindings']['object']['entity_id'].startswith('concept-')

    counts = classification_counts([row], complete_ref)
    assert counts['classified_endpoint_mentions'] == 1
    assert counts['endpoint_review_warning_mentions'] == 1
    assert counts['mapping_review_warning_claims'] == 1
    assert counts['classified_claims'] == 0
    assert counts['classification_review_claims'] == 1

    entities = entity_registry([row])
    obj_ent = next(e for e in entities if e['iri'] == BASE + 'GO_0003993')
    assert obj_ent['status'] == 'alignment_review'
    assert obj_ent['alignment_category'] == 'review'
    assert obj_ent['shared_concept'] is False

    # Non-quantitative functional attribution without modulation/assay framing must not false-positive.
    capability = _row(
        'acp-function',
        subj_iri=BASE + 'PO_0009005',
        obj_iri=BASE + 'GO_0003993',
        rel_iri=BASE + 'RO_0000086',
        rel_mapping='broader',
        subj_mapping='exact',
        obj_mapping='exact',
    )
    capability['original_claim'] = {
        'subject': 'acid phosphatase enzyme',
        'relation': 'catalyzes',
        'object': 'acid phosphatase activity',
        'qualifiers': {},
        'source_evidence_span': 'Purified isoform exhibits canonical catalytic hydrolysis.',
    }
    assert not mapping_meaning_warnings(capability, complete_ref)


def test_live_entity_viewer_excludes_warned_endpoint_from_grounded_and_classified(tmp_path, monkeypatch, reference):
    from backend.shared import ontology_browser
    terms = deepcopy(reference.terms)
    terms[BASE + 'GO_0003993']['parents'] = [BASE + 'BFO_0000002']
    terms[BASE + 'PO_0009005']['parents'] = [BASE + 'BFO_0000002']
    complete_ref = SimpleNamespace(
        terms=terms, profile=reference.profile, graph=reference.graph,
        signature=reference.signature, rules_signature=reference.rules_signature,
        children=reference.children, manifest=reference.manifest,
        selectable_counts=reference.selectable_counts, all_counts=reference.all_counts,
        canonical_equivalent=reference.canonical_equivalent,
    )
    row = _row(
        'live-acp',
        subj_iri=BASE + 'PO_0009005',
        obj_iri=BASE + 'GO_0003993',
        rel_iri=BASE + 'RO_0002566',
        rel_mapping='exact',
        subj_mapping='exact',
        obj_mapping='exact',
    )
    row['document_key'] = 'doc'
    row['original_claim'] = {
        'subject': 'root',
        'relation': 'increased',
        'object': 'acid phosphatase activity',
        'qualifiers': {},
        'source_evidence_span': 'Root growth increased acid phosphatase activity in roots.',
    }
    refresh_eligibility(row, complete_ref)
    snapshot = {
        'catalog_signature': complete_ref.signature,
        'rules_signature': complete_ref.rules_signature,
        'assertions': [row],
        'entities': entity_registry([row]),
        'counts': {},
    }
    monkeypatch.setattr(ontology_browser, 'catalog', lambda: complete_ref)
    workspace = SimpleNamespace(ONTOLOGY_DIR=tmp_path, _lock=RLock(), _jobs={})
    app = FastAPI()
    app.include_router(create_router(workspace, read_only=True,
        snapshot_loader=lambda path, default: snapshot if path.name == 'snapshot.json' else default))
    with TestClient(app) as client:
        grounded = client.get('/api/ontology/entities?status=grounded').json()
        assert grounded['total'] == 1 and grounded['entities'][0]['iri'] == BASE + 'PO_0009005'
        classified = client.get('/api/ontology/entities?status=classified').json()
        assert classified['total'] == 1 and classified['entities'][0]['iri'] == BASE + 'PO_0009005'
        aligned = client.get('/api/ontology/entities?status=alignment_review').json()
        assert aligned['total'] == 1 and aligned['entities'][0]['iri'] == BASE + 'GO_0003993'
        reviewed = client.get('/api/ontology/entities?status=review').json()
        assert reviewed['total'] == 1 and reviewed['entities'][0]['iri'] == BASE + 'GO_0003993'


def test_live_entity_viewer_counts_retrieved_non_seed_classes_in_classified(tmp_path, monkeypatch, reference):
    from backend.ontology.inventory_policy import SIGNATURE as INVENTORY_SIGNATURE
    from backend.shared import ontology_browser

    foodon_potato = BASE + 'FOODON_03315354'
    cdno_diet_p = BASE + 'CDNO_0000018'
    row = _row(
        'live-retrieved-food',
        subj_iri=foodon_potato,
        obj_iri=cdno_diet_p,
        rel_iri=BASE + 'BFO_0000051',
        rel_mapping='exact',
        subj_mapping='exact',
        obj_mapping='broader',
    )
    row['document_key'] = 'doc'
    row['original_claim'] = {
        'subject': 'potato',
        'relation': 'has part',
        'object': 'phosphorus',
        'qualifiers': {},
        'source_evidence_span': 'Potatoes contain phosphorus.',
    }
    row['bindings']['subject']['retrieved_term'] = {'iri': foodon_potato, 'label': 'potato', 'kind': 'class', 'inventory_signature': INVENTORY_SIGNATURE}
    row['bindings']['object']['retrieved_term'] = {'iri': cdno_diet_p, 'label': 'dietary phosphorus', 'kind': 'class', 'inventory_signature': INVENTORY_SIGNATURE}
    refresh_eligibility(row, reference)
    snapshot = {
        'catalog_signature': reference.signature,
        'rules_signature': reference.rules_signature,
        'assertions': [row],
        'entities': entity_registry([row]),
        'counts': {},
    }
    monkeypatch.setattr(ontology_browser, 'catalog', lambda: reference)
    workspace = SimpleNamespace(ONTOLOGY_DIR=tmp_path, _lock=RLock(), _jobs={})
    app = FastAPI()
    app.include_router(create_router(workspace, read_only=True,
        snapshot_loader=lambda path, default: snapshot if path.name == 'snapshot.json' else default))
    with TestClient(app) as client:
        data = client.get('/api/ontology/entities?status=classified').json()
        assert data['status_counts']['grounded'] == 1
        assert data['status_counts']['classification'] == 1
        assert data['status_counts']['classified'] == 2
        assert data['total'] == 2
        terms = client.get('/api/ontology/terms?kind=class&limit=5').json()['terms']
        assert {t['iri'] for t in terms[:2]} == {foodon_potato, cdno_diet_p}


def test_ground_claims_persists_property_type_check(tmp_path, reference):
    c = claim('g1', subject='root', relation='part of', object='root system')
    rows = ground_claims([c], tmp_path, reference, Grounder())
    assert rows[0]['property_type_check']['status'] == 'compatible'
    assert rows[0]['property_type_policy'] == PROPERTY_TYPE_POLICY
    assert rows[0]['assembly_eligible'] is True


def test_regulatory_limit_and_agent_role_reversal_guards(reference):
    # 1. Permitted maximum limit ("can contain up to 5,000 mg/kg") must not count as measured compositional has_part
    reg_row = _row(
        'reg-limit-1',
        subj_iri=BASE + 'PO_0009005',
        obj_iri=BASE + 'CHEBI_26020',
        rel_iri=BASE + 'BFO_0000051',
        rel_mapping='broader',
    )
    reg_row['original_claim']['subject'] = 'processed potato products'
    reg_row['original_claim']['relation'] = 'can contain up to'
    reg_row['original_claim']['object'] = 'phosphate additives'
    reg_row['original_claim']['source_evidence_span'] = (
        'Processed potato products in the sense of Regulation (EC) No 1333/2008 '
        'can contain up to 5,000 mg/kg of phosphate additives.'
    )
    warns = mapping_meaning_warnings(reg_row, reference)
    assert [w['code'] for w in warns] == ['regulatory_limit_mapped_as_measured_content']
    assert warns[0]['role'] == 'relation'
    assert not classification_complete(reg_row, reference)
    assert not assertion_eligible(reg_row, reference)

    # Measured physical composition ("contain particularly high levels of") does NOT trigger regulatory warning
    meas_row = _row(
        'meas-content-1',
        subj_iri=BASE + 'PO_0009005',
        obj_iri=BASE + 'CHEBI_26020',
        rel_iri=BASE + 'BFO_0000051',
        rel_mapping='broader',
    )
    meas_row['original_claim']['subject'] = 'foods rich in phosphate additives'
    meas_row['original_claim']['relation'] = 'contain particularly high levels of'
    meas_row['original_claim']['object'] = 'inorganic phosphate'
    meas_row['original_claim']['source_evidence_span'] = (
        'Foods that are rich in phosphate additives contain particularly high levels of inorganic phosphate.'
    )
    assert mapping_meaning_warnings(meas_row, reference) == []
    assert classification_complete(meas_row, reference)

    # 2. Agent role reversal: consumed substance in subject of active "consumes"
    bad_consume = {
        'subject': 'phosphate',
        'relation': 'chronically consumes at least twice the recommended amount of',
        'object': 'Acceptable Daily Intake',
        'qualifiers': {},
        'source_evidence_fragments': [{
            'sentence_number': 1,
            'text': 'A significant proportion of the population chronically consumes at least twice the recommended amount of phosphate.'
        }],
    }
    assert any(w['code'] == 'possible_agent_role_reversal' for w in extraction_warnings(bad_consume))

    good_consume = {
        'subject': 'younger population groups',
        'relation': 'chronically consume',
        'object': 'phosphate',
        'qualifiers': {},
        'source_evidence_fragments': [{
            'sentence_number': 1,
            'text': 'Younger population groups chronically consume at least twice the recommended amount of phosphate.'
        }],
    }
    assert not any(w['code'] == 'possible_agent_role_reversal' for w in extraction_warnings(good_consume))

    # 3. Passive addition reversal: recipient food in subject of "is not permitted to be added to"
    bad_addition = {
        'subject': 'unprocessed potatoes',
        'relation': 'is not permitted to be added to',
        'object': 'phosphate additives',
        'qualifiers': {},
        'source_evidence_fragments': [{
            'sentence_number': 1,
            'text': 'whereas the addition of phosphate additives is not permitted in the case of unprocessed potatoes.'
        }],
    }
    assert any(w['code'] == 'possible_agent_role_reversal' for w in extraction_warnings(bad_addition))

    good_addition = {
        'subject': 'phosphate additives',
        'relation': 'are not permitted to be added to',
        'object': 'unprocessed potatoes',
        'qualifiers': {},
        'source_evidence_fragments': [{
            'sentence_number': 1,
            'text': 'whereas the addition of phosphate additives is not permitted in the case of unprocessed potatoes.'
        }],
    }
    assert not any(w['code'] == 'possible_agent_role_reversal' for w in extraction_warnings(good_addition))


@pytest.mark.parametrize('cid,subj,rel,obj,target_role,target_iri,target_label,mapping,ancestors', [
    (
        'hp-urinary', 'phosphate supplemented diet', 'positively regulates characteristic', 'urinary phosphate excretion',
        'object', BASE + 'HP_0003109', 'Hyperphosphaturia', 'exact',
        [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'PATO_0000001', BASE + 'HP_0000118'],
    ),
    (
        'hp-fgf23', 'excessive phosphate intake', 'positively regulates characteristic', 'circulating fibroblast growth factor 23 levels',
        'object', BASE + 'HP_6000407', 'Elevated circulating fibroblast growth factor 23 concentration', 'exact',
        [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'PATO_0000001', BASE + 'HP_0000118'],
    ),
    (
        'hp-fgf23-inc', 'circulating fibroblast growth factor 23 increase', 'causally upstream of', 'phosphate reabsorption suppression',
        'subject', BASE + 'HP_6000407', 'Elevated circulating fibroblast growth factor 23 concentration', 'exact',
        [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'PATO_0000001', BASE + 'HP_0000118'],
    ),
    (
        'hp-serum-broader', 'serum phosphate levels', 'correlated with', 'carotid intimal-medial thickness',
        'subject', BASE + 'HP_0100529', 'Abnormal circulating phosphate ion concentration', 'broader',
        [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'PATO_0000001', BASE + 'HP_0000118'],
    ),
    (
        'hp-carotid-exact', 'serum phosphate levels', 'correlated with', 'carotid intimal-medial thickness',
        'object', BASE + 'HP_0012273', 'Increased carotid artery intimal medial thickness', 'exact',
        [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'PATO_0000001', BASE + 'HP_0000118'],
    ),
    (
        'hp-blood', 'reduced kidney function', 'positively regulates characteristic', 'blood phosphate levels',
        'object', BASE + 'HP_0002905', 'Hyperphosphatemia', 'exact',
        [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'PATO_0000001', BASE + 'HP_0000118'],
    ),
])
def test_neutral_measurement_to_abnormal_hp_is_blocked_for_retrieved_exact_and_broader(
    reference, cid, subj, rel, obj, target_role, target_iri, target_label, mapping, ancestors,
):
    from backend.domain.classification import endpoint_accepted
    from backend.ontology.inventory_policy import SIGNATURE

    other_role = 'subject' if target_role == 'object' else 'object'
    row = _row(
        cid,
        subj_iri=target_iri if target_role == 'subject' else BASE + 'PECO_0007397',
        obj_iri=target_iri if target_role == 'object' else BASE + 'TO_0000387',
        rel_iri=BASE + 'RO_0002610',
        rel_mapping='exact',
        subj_mapping=mapping if target_role == 'subject' else 'broader',
        obj_mapping=mapping if target_role == 'object' else 'broader',
    )
    row['document_key'] = 'doc'
    row['original_claim'].update({'subject': subj, 'relation': rel, 'object': obj})
    row['bindings'][target_role].update({
        'label': target_label,
        'verified_ancestor_iris': ancestors,
        'retrieved_term': {'kind': 'class', 'iri': target_iri, 'label': target_label, 'inventory_signature': SIGNATURE},
    })
    row['bindings'][other_role].update({
        'iri': BASE + 'TO_0000387',
        'label': 'plant trait',
        'canonical_iri': BASE + 'TO_0000387',
        'verified_ancestor_iris': [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'PATO_0000001'],
        'retrieved_term': {'kind': 'class', 'iri': BASE + 'TO_0000387', 'label': 'plant trait', 'inventory_signature': SIGNATURE},
    })
    if rel == 'causally upstream of':
        row['bindings']['relation']['iri'] = BASE + 'RO_0019001'
    refresh_eligibility(row, reference)
    warns = [w for w in row['mapping_review_warnings'] if w['role'] == target_role]
    assert [w['code'] for w in warns] == ['neutral_measurement_mapped_to_abnormal_phenotype']
    assert not assertion_eligible(row, reference)
    assert not classification_complete(row, reference)
    assert not endpoint_accepted(row, target_role, reference)


def test_polarity_inversion_health_to_disease_and_valid_abnormality_controls(reference):
    from backend.ontology.inventory_policy import SIGNATURE

    inv_row = _row(
        'health-to-disease',
        subj_iri=BASE + 'CDNO_0200578',
        obj_iri=BASE + 'MONDO_1011909',
        rel_iri=BASE + 'RO_0002566',
        rel_mapping='exact',
        subj_mapping='broader',
        obj_mapping='narrower',
    )
    inv_row['original_claim'].update({'subject': 'nutritional phosphorus excess', 'relation': 'causally influences', 'object': 'renal health'})
    inv_row['bindings']['object'].update({
        'label': 'renal disease, non-human animal',
        'verified_ancestor_iris': [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'MONDO_0000001'],
        'retrieved_term': {'kind': 'class', 'iri': BASE + 'MONDO_1011909', 'label': 'renal disease, non-human animal', 'inventory_signature': SIGNATURE},
    })
    assert [w['code'] for w in mapping_meaning_warnings(inv_row, reference)] == ['polarity_inversion_health_to_disease']

    # Genuine abnormality / clinical endpoints must NOT trigger false positives.
    for ep_text, ep_iri, ep_label, mapping, anc in [
        ('bone demineralization', BASE + 'HP_0004349', 'Reduced bone mineral density', 'broader', [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'PATO_0000001', BASE + 'HP_0000118']),
        ('renal calcification', BASE + 'HP_0000121', 'Nephrocalcinosis', 'exact', [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'PATO_0000001', BASE + 'HP_0000118']),
        ('reduced kidney function', BASE + 'HP_0000083', 'Renal insufficiency', 'broader', [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'PATO_0000001', BASE + 'HP_0000118']),
        ('kidney damage', BASE + 'HP_0000112', 'Nephropathy', 'broader', [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'PATO_0000001', BASE + 'HP_0000118']),
        ('interstitial fibrosis', BASE + 'HP_0030760', 'Renal fibrosis', 'broader', [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'PATO_0000001', BASE + 'HP_0000118']),
        ('nephrocalcinosis', BASE + 'MONDO_0001567', 'nephrocalcinosis', 'exact', [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'MONDO_0000001']),
    ]:
        ctrl = _row(
            f'ctrl-{ep_iri.rsplit("/", 1)[-1]}',
            subj_iri=ep_iri,
            obj_iri=BASE + 'TO_0000387',
            rel_iri=BASE + 'RO_0002610',
            rel_mapping='exact',
            subj_mapping=mapping,
            obj_mapping='broader',
        )
        ctrl['original_claim'].update({'subject': ep_text, 'relation': 'correlated with', 'object': 'phosphorus load'})
        ctrl['bindings']['subject'].update({
            'label': ep_label,
            'verified_ancestor_iris': anc,
            'retrieved_term': {'kind': 'class', 'iri': ep_iri, 'label': ep_label, 'inventory_signature': SIGNATURE},
        })
        assert mapping_meaning_warnings(ctrl, reference) == []


def test_parthood_category_mismatch_blocks_material_to_quality_and_keeps_valid_controls(reference):
    from backend.ontology.inventory_policy import SIGNATURE

    # Material continuant (FOODON potato) --(has part)--> quality/trait (TO phosphorus concentration) must be held.
    bad_part = _row(
        'pandey-bad-part',
        subj_iri=BASE + 'FOODON_03315354',
        obj_iri=BASE + 'TO_0006045',
        rel_iri=BASE + 'BFO_0000051',
        rel_mapping='exact',
        subj_mapping='exact',
        obj_mapping='broader',
    )
    bad_part['document_key'] = 'doc'
    bad_part['original_claim'].update({
        'subject': 'potato tuber',
        'relation': 'has part',
        'object': 'phosphorus',
        'source_evidence_span': 'Major minerals in potato tubers include nitrogen (N), phosphorus (P).',
    })
    bad_part['bindings']['subject'].update({
        'label': 'potato',
        'verified_ancestor_iris': [BASE + 'BFO_0000002', BASE + 'BFO_0000004', BASE + 'BFO_0000040', BASE + 'FOODON_00002403'],
        'retrieved_term': {'kind': 'class', 'iri': BASE + 'FOODON_03315354', 'label': 'potato', 'inventory_signature': SIGNATURE},
    })
    bad_part['bindings']['object'].update({
        'label': 'phosphorus concentration',
        'verified_ancestor_iris': [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'PATO_0000001', BASE + 'TO_0000387'],
        'retrieved_term': {'kind': 'class', 'iri': BASE + 'TO_0006045', 'label': 'phosphorus concentration', 'inventory_signature': SIGNATURE},
    })
    refresh_eligibility(bad_part, reference)
    assert bad_part['property_type_check']['status'] == 'compatible'
    assert [w['code'] for w in bad_part['mapping_review_warnings']] == ['parthood_category_mismatch']
    assert bad_part['mapping_review_warnings'][0]['role'] == 'object'
    assert not assertion_eligible(bad_part, reference)
    assert not classification_complete(bad_part, reference)

    # Valid controls: FOODON -> FOODON, FOODON -> CHEBI, PO -> PO must remain eligible and unwarned.
    for cid, s_iri, s_lbl, s_anc, o_iri, o_lbl, o_anc in [
        (
            'food-to-additive',
            BASE + 'FOODON_00001002', 'food product', [BASE + 'BFO_0000002', BASE + 'BFO_0000004', BASE + 'BFO_0000040', BASE + 'FOODON_00002403'],
            BASE + 'FOODON_03412972', 'food additive', [BASE + 'BFO_0000002', BASE + 'BFO_0000004', BASE + 'BFO_0000040', BASE + 'FOODON_00002403'],
        ),
        (
            'food-to-chebi',
            BASE + 'FOODON_00002403', 'food material', [BASE + 'BFO_0000002', BASE + 'BFO_0000004', BASE + 'BFO_0000040'],
            BASE + 'CHEBI_24838', 'inorganic phosphate', [],
        ),
        (
            'po-to-po',
            BASE + 'PO_0025025', 'root system', [BASE + 'BFO_0000002', BASE + 'BFO_0000004', BASE + 'BFO_0000040'],
            BASE + 'PO_0020121', 'lateral root', [BASE + 'BFO_0000002', BASE + 'BFO_0000004', BASE + 'BFO_0000040'],
        ),
    ]:
        ok = _row(cid, subj_iri=s_iri, obj_iri=o_iri, rel_iri=BASE + 'BFO_0000051', rel_mapping='exact', subj_mapping='broader', obj_mapping='exact')
        ok['document_key'] = 'doc'
        ok['original_claim'].update({'subject': s_lbl, 'relation': 'has part', 'object': o_lbl})
        ok['bindings']['subject'].update({
            'label': s_lbl,
            'verified_ancestor_iris': s_anc,
            'retrieved_term': {'kind': 'class', 'iri': s_iri, 'label': s_lbl, 'inventory_signature': SIGNATURE},
        })
        ok['bindings']['object'].update({
            'label': o_lbl,
            'verified_ancestor_iris': o_anc,
            'retrieved_term': {'kind': 'class', 'iri': o_iri, 'label': o_lbl, 'inventory_signature': SIGNATURE},
        })
        refresh_eligibility(ok, reference)
        assert ok['mapping_review_warnings'] == []
        assert assertion_eligible(ok, reference)
        assert classification_complete(ok, reference)


def test_ro_12_condition_and_process_regulation_checks(reference):
    from backend.ontology.inventory_policy import SIGNATURE

    # P10 (RO_0003302) accepts ECTO->HP, ECTO->MONDO, and PR->HP, while holding neutral CMO/OBA objects.
    for cid, s_iri, s_lbl, s_anc, o_iri, o_lbl, o_anc in [
        (
            'ecto-hp-p10',
            BASE + 'ECTO_0900088', 'exposure to phosphorus via ingestion', [BASE + 'BFO_0000003', BASE + 'BFO_0000015'],
            BASE + 'HP_0000121', 'Nephrocalcinosis', [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'PATO_0000001', BASE + 'HP_0000118'],
        ),
        (
            'ecto-mondo-p10',
            BASE + 'ECTO_0900088', 'exposure to phosphorus via ingestion', [BASE + 'BFO_0000003', BASE + 'BFO_0000015'],
            BASE + 'MONDO_0005300', 'chronic kidney disease', [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'MONDO_0000001'],
        ),
        (
            'pr-hp-p10',
            BASE + 'PR_000007493', 'fibroblast growth factor 23', [BASE + 'BFO_0000002', BASE + 'BFO_0000004', BASE + 'BFO_0000040'],
            BASE + 'HP_0001717', 'Coronary artery calcification', [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'PATO_0000001', BASE + 'HP_0000118'],
        ),
    ]:
        row = _row(cid, subj_iri=s_iri, obj_iri=o_iri, rel_iri=BASE + 'RO_0003302', rel_mapping='exact', subj_mapping='broader', obj_mapping='exact')
        row['document_key'] = 'doc'
        row['original_claim'].update({'subject': s_lbl, 'relation': 'causes or contributes to condition', 'object': o_lbl})
        row['bindings']['subject'].update({'label': s_lbl, 'verified_ancestor_iris': s_anc,
                                           'retrieved_term': {'kind': 'class', 'iri': s_iri, 'label': s_lbl, 'inventory_signature': SIGNATURE}})
        row['bindings']['object'].update({'label': o_lbl, 'verified_ancestor_iris': o_anc,
                                          'retrieved_term': {'kind': 'class', 'iri': o_iri, 'label': o_lbl, 'inventory_signature': SIGNATURE}})
        refresh_eligibility(row, reference)
        assert row['property_type_check']['status'] == 'compatible'
        assert row['mapping_review_warnings'] == []
        assert assertion_eligible(row, reference)

    bad_neutral = _row(
        'p10-neutral-cmo',
        subj_iri=BASE + 'ECTO_0900088',
        obj_iri=BASE + 'CMO_0000542',
        rel_iri=BASE + 'RO_0003302',
        rel_mapping='exact',
        subj_mapping='broader',
        obj_mapping='exact',
    )
    bad_neutral['document_key'] = 'doc'
    bad_neutral['original_claim'].update({'subject': 'excess phosphate intake', 'relation': 'causes or contributes to condition', 'object': 'serum phosphate level'})
    bad_neutral['bindings']['subject'].update({'label': 'exposure to phosphorus via ingestion', 'verified_ancestor_iris': [BASE + 'BFO_0000003', BASE + 'BFO_0000015'],
                                               'retrieved_term': {'kind': 'class', 'iri': BASE + 'ECTO_0900088', 'label': 'exposure to phosphorus via ingestion', 'inventory_signature': SIGNATURE}})
    bad_neutral['bindings']['object'].update({'label': 'serum phosphate level', 'verified_ancestor_iris': [BASE + 'BFO_0000002', BASE + 'BFO_0000020', BASE + 'PATO_0000001', BASE + 'CMO_0000000'],
                                              'retrieved_term': {'kind': 'class', 'iri': BASE + 'CMO_0000542', 'label': 'serum phosphate level', 'inventory_signature': SIGNATURE}})
    refresh_eligibility(bad_neutral, reference)
    assert [w['code'] for w in bad_neutral['mapping_review_warnings']] == ['condition_property_with_neutral_measurement_object']
    assert not assertion_eligible(bad_neutral, reference)

    # P11 (RO_0002213) and P12 (RO_0002212) accept process -> process pairs.
    proc_row = _row(
        'peco-go-p11',
        subj_iri=BASE + 'PECO_0007246',
        obj_iri=BASE + 'GO_0006817',
        rel_iri=BASE + 'RO_0002213',
        rel_mapping='exact',
        subj_mapping='broader',
        obj_mapping='exact',
    )
    proc_row['document_key'] = 'doc'
    proc_row['original_claim'].update({'subject': 'PGPR treatment', 'relation': 'positively regulates', 'object': 'phosphate ion transport'})
    proc_row['bindings']['subject'].update({'label': 'bacterial plant exposure', 'verified_ancestor_iris': [BASE + 'BFO_0000003', BASE + 'BFO_0000015'],
                                            'retrieved_term': {'kind': 'class', 'iri': BASE + 'PECO_0007246', 'label': 'bacterial plant exposure', 'inventory_signature': SIGNATURE}})
    proc_row['bindings']['object'].update({'label': 'phosphate ion transport', 'verified_ancestor_iris': [BASE + 'BFO_0000003', BASE + 'BFO_0000015'],
                                           'retrieved_term': {'kind': 'class', 'iri': BASE + 'GO_0006817', 'label': 'phosphate ion transport', 'inventory_signature': SIGNATURE}})
    refresh_eligibility(proc_row, reference)
    assert proc_row['property_type_check']['status'] == 'compatible'
    assert proc_row['mapping_review_warnings'] == []
    assert assertion_eligible(proc_row, reference)
