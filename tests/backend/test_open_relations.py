"""Semantic concerns cannot discard or rewrite source-anchored extraction output."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from backend.config.settings import Settings
from backend.domain.ontology import schema_error
from backend.providers.response_repair import validate_schema
from backend.providers.structured_output import SchemaValidationError
from backend.stages.claim_extraction.contract import EXTRACTION_RESPONSE_SCHEMA
from backend.stages.claim_extraction.extractor import extract_claims
from backend.stages.external_validation.joint import judge_evidence_pack
from backend.stages.graph_construction.graph import graph_from_candidates
from tests.backend.test_extraction_targets import row, packet
from tests.external.test_joint_validation import fixture as external_fixture


def proposed(**changes):
    item = dict(claim_text='Stripe rust has more aggressive strains adapted to higher temperatures.',
                source_context='', subject='stripe rust', subject_source_mention='Stripe rust',
                relation='has_trait', relation_trigger='has',
                object='more aggressive strains adapted to higher temperatures',
                object_source_mention='more aggressive strains adapted to higher temperatures',
                subject_type='PlantDisease', object_type='PlantTrait',
                certainty='descriptive', evidence_sentence_numbers=[1],
                qualifiers={k:'' for k in ('crop_or_species','population','condition','comparator','metric')})
    return {**item, **changes}


def extract(tmp_path, items, text=None):
    text = text or 'Stripe rust has more aggressive strains adapted to higher temperatures.'
    pdf = tmp_path / 'fixture.pdf'
    pdf.write_bytes(b'Parsed fixture')

    class Client:
        def chat_json(self, system, user, **options):
            result = {'claims': deepcopy(items)}
            validate_schema(result, options['json_schema'])
            options['response_check'](result)
            return result

    with patch('backend.stages.claim_extraction.extractor.source_paper_title', return_value='Fixture'):
        return extract_claims(pdf, Client(), 0, lambda *args:None, packets=[packet([row(1,text=text)])],
                              source_identifiers={'source_doi':'fixture'}, bibliography=[], extraction_mode='target_context')


def test_stripe_rust_type_pair_passes_structure_without_becoming_scientifically_approved(tmp_path):
    item = proposed()
    candidates, rejected, _ = extract(tmp_path, [item])
    assert len(candidates) == 1 and not rejected
    claim = candidates[0]
    assert claim['schema_error'] is None
    for key in ('subject','object','relation','subject_type','object_type','certainty','qualifiers'):
        assert claim[key] == item[key]
    assert not claim.get('source_validation') and not claim.get('validation_status')


@pytest.mark.parametrize('relation', ['causes','associated_with','higher_than','produces','adapts_to','colonizes'])
def test_relation_names_are_not_an_extraction_whitelist(tmp_path, relation):
    candidates, rejected, _ = extract(tmp_path,[proposed(relation=relation)])
    assert len(candidates) == 1 and not rejected
    assert candidates[0]['relation'] == relation and candidates[0]['schema_error'] is None


def test_semantic_hints_retain_endpoints_conditions_and_modality_without_local_corrections(tmp_path):
    text = 'Soil can be associated with lower disease intensity than treated soil.'
    item = proposed(subject='Soil', subject_source_mention='Soil', subject_type='SoilMaterial',
                    object='disease intensity', object_source_mention='disease intensity',
                    relation='reduces', relation_trigger='associated with lower', certainty='descriptive',
                    claim_text=text, qualifiers={'crop_or_species':'Soil', 'population':'farming in Region S',
                                                'condition':'treated soil', 'comparator':'', 'metric':''})
    candidates, rejected, _ = extract(tmp_path,[item],text)
    assert len(candidates) == 1 and not rejected
    claim = candidates[0]
    assert claim['qualifiers'] == item['qualifiers']
    assert claim['certainty'] == 'descriptive' and not claim['source_context']
    assert {'review_generic_entity','review_association_and_direction','review_comparator_or_metric',
            'review_source_modality'} <= set(claim['extraction_review_hints'])
    assert not claim['extraction_normalizations'] and not claim['qualifier_adjustments']


def test_punctuation_in_a_chemical_name_is_not_a_semantic_rejection(tmp_path):
    text = '2,4-D reduces root growth.'
    item = proposed(subject='2,4-D',subject_source_mention='2,4-D',subject_type='Contaminant',
                    relation='reduces',relation_trigger='reduces',object='root growth',
                    object_source_mention='root growth',object_type='PlantProcess',claim_text=text)
    candidates, rejected, _ = extract(tmp_path,[item],text)
    assert len(candidates) == 1 and not rejected


def test_same_span_with_different_restrictions_is_not_deduplicated(tmp_path):
    first = proposed()
    second = proposed(qualifiers={**first['qualifiers'], 'condition':'new strains'})
    candidates, rejected, _ = extract(tmp_path,[first,second])
    assert len(candidates) == 2 and not rejected
    assert candidates[0]['qualifiers'] != candidates[1]['qualifiers']


def test_same_assertion_with_different_source_context_has_distinct_stable_ids(tmp_path):
    text = 'Stripe rust has more aggressive strains adapted to higher temperatures in Region A and Region B.'
    items = [proposed(source_context=context, claim_text=f'Stripe rust has these strains in {context}.')
             for context in ('Region A', 'Region B')]
    candidates, rejected, _ = extract(tmp_path, items, text)
    assert len(candidates) == 2 and not rejected
    assert len({c['candidate_id'] for c in candidates}) == 2
    assert [c['source_context'] for c in candidates] == ['Region A', 'Region B']
    repeated, _, _ = extract(tmp_path, items, text)
    assert [c['candidate_id'] for c in repeated] == [c['candidate_id'] for c in candidates]


@pytest.mark.parametrize('changes', [{'subject_source_mention':'invented actor'},
                                   {'relation_trigger':'causes'}, {'evidence_sentence_numbers':[2]}])
def test_provenance_failures_still_cannot_enter_the_pipeline(tmp_path, changes):
    if 'evidence_sentence_numbers' in changes:
        candidates, _, _ = extract(tmp_path,[proposed(**changes)])
        assert not candidates
    else:
        candidates, rejected, _ = extract(tmp_path,[proposed(**changes)])
        assert not candidates and rejected['unanchored_evidence'] == 1


@pytest.mark.parametrize('changes', [{'relation':''},{'relation':' '},{'relation':42},
                                   {'subject_type':None},{'qualifiers':[]}, {'evidence_sentence_numbers':['1']}])
def test_format_contract_remains_strict(changes):
    with pytest.raises(SchemaValidationError):
        validate_schema({'claims':[proposed(**changes)]},EXTRACTION_RESPONSE_SCHEMA)


def test_unresolved_type_is_provisional_and_never_forced_into_a_crop_label(tmp_path):
    candidates, rejected, _ = extract(tmp_path,[proposed(subject_type='Unresolved')])
    assert not rejected and candidates[0]['subject_type'] == 'Unresolved'
    assert schema_error(candidates[0]) is None


def test_source_proof_cannot_invent_relation_wording_even_when_extractor_anchor_exists():
    from backend.stages.source_validation.checker import validate_source_claim
    from tests.backend.test_source_validation import FakeClient
    claim = proposed()
    claim.update(candidate_id='fixture',source_evidence_span=claim['claim_text'],source_evidence_sentence_numbers=[1])
    response = {'decision':'entailed','relation_match':True,'direction_match':True,'certainty_match':True,
                'proof':{'subject':claim['subject_source_mention'],'object':claim['object_source_mention'],
                         'relation':'invented wording'},'rationale':'Untrusted model assertion.'}
    result = validate_source_claim(claim,FakeClient(response))
    assert result['decision'] == 'needs_review'
    assert 'relation proof is missing from the selected evidence' in result['reasons']


@pytest.mark.parametrize('scope,unresolved',[(False,False),(True,True)])
def test_external_scope_or_unresolved_type_prevents_trusted_graph_admission(scope,unresolved):
    claim, documents, response, client = external_fixture()
    response['semantic_assessment']['in_scope'] = scope
    if unresolved:
        claim['subject_type'] = response['semantic_assessment']['subject_type'] = 'Unresolved'
    # This is an externally judged scope/type result, not a local keyword rule.
    decisions, assessment = judge_evidence_pack(client,claim,documents,Settings())
    assert decisions[0]['decision'] == 'needs_review' and assessment['semantic_review_required']
    graph = graph_from_candidates([claim],[{'candidate_id':claim['candidate_id'],'attempts':[{'decisions':decisions}]}])
    assert not graph['edges']


@pytest.mark.parametrize('contradiction',[False,True])
def test_novel_predicate_reaches_external_model_and_can_have_guarded_support_or_conflict(contradiction):
    claim, documents, response, client = external_fixture()
    claim.update(subject='Pathogen P',subject_type='PlantPathogen',relation='produces',object='toxin T',object_type='Contaminant')
    response['semantic_assessment'].update(subject_type='PlantPathogen',object_type='Contaminant')
    for document, decision in zip(documents,response['documents']):
        text = 'Pathogen P produces toxin T.'
        document['evidence_chunks'][0]['text'] = text
        decision['decision'] = 'supported'
        decision['matches'] = {k:True for k in ('subject','object','relation','direction','certainty')}
        decision['evidence'][0].update(subject_mention='Pathogen P',object_mention='toxin T',relation_trigger='produces',stance='supports')
        for role,entity in [('subject','Pathogen P'),('object','toxin T')]:
            decision['entity_alignment'][role].update(canonical=entity,mention=entity,evidence=text)
    if contradiction:
        text = 'Pathogen P does not produce toxin T.'
        documents[1]['evidence_chunks'][0]['text'] = text
        decision = response['documents'][1]
        decision['decision'] = 'contradicted'
        decision['matches']['direction'] = False
        decision['evidence'][0].update(relation_trigger='does not produce',stance='contradicts')
        for binding in decision['entity_alignment'].values():
            binding['evidence'] = text
        decision['rationale'] = 'The same organism explicitly did not produce this toxin under the matched conditions.'
    for role in response['semantic_assessment']['role_evidence']:
        role.update(entity_mention=claim[role['role']],role_trigger='produces toxin T')
    decisions, assessment = judge_evidence_pack(client,claim,documents,Settings())
    assert [d['decision'] for d in decisions] == ['supported','contradicted' if contradiction else 'supported']
    assert not assessment['semantic_review_required']
    payload = json.loads(client.chat_json.call_args.args[1])
    assert payload['claim']['relation'] == 'produces'


def test_explicit_external_scope_exclusion_is_enforced_by_graph_and_global_assembly(tmp_path):
    from backend.shared.artifacts import save_rows
    from backend.stages.pathway_assembly.corpus import collect_corpus
    from tests.backend.test_pathway_assembly import saved_claim
    folder, claim = saved_claim(tmp_path, 'outside domain', 'A drug', 'improves', 'recovery')
    result = {'candidate_id':claim['candidate_id'], 'attempts':[{
        'joint_assessment':{'semantic_assessment':{'in_scope':False}},
        'decisions':[{'evidence_document_id':'p','decision':'supported'}]}],
        'support_summary':{'independent_groups':2}}
    save_rows(folder/'validation_runs.jsonl',[result])
    assert not graph_from_candidates([claim],[result])['edges']
    assert collect_corpus(tmp_path) == ([],[])
    # The original extraction remains inspectable in its source/audit layer.
    assert graph_from_candidates([claim],[result],view='audit')['edges']
    assert graph_from_candidates([claim],[result],view='source')['edges']
    result['attempts'] = []
    save_rows(folder/'validation_runs.jsonl',[result])
    assert not collect_corpus(tmp_path)[0]  # No accepted paper support remains.
