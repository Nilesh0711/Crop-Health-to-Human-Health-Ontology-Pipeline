"""Source-bound identity, cross-batch diagnostics and strict cache compatibility."""
from copy import deepcopy
import json

import pytest

from backend.domain.extraction_review import refresh_eligibility, review_status
from backend.ontology.policies import ENTITY_GROUNDING_POLICY
from backend.stages.claim_extraction.anchoring import endpoint_anchor, coordination_spans, ANCHORING_POLICY
from backend.stages.claim_extraction.contract import EXTRACTION_CONTRACT_VERSION
from backend.stages.claim_extraction.extractor import _load_cached_packet_claims, extract_claims
from backend.stages.claim_extraction.targeting import TARGETING_VERSION, READING_CONTEXT_POLICY
from backend.stages.ontology_grounding.grouping import flag_consistency, CONSISTENCY_POLICY
from backend.stages.pdf_parsing.layout import PARSING_POLICY
from backend.stages.pdf_parsing.packets import page_sentences
from tests.backend.test_extraction_targets import packet, row


@pytest.mark.parametrize('entity,anchor,source,expected', [
    ('invented Trait Z', 'Trait Q', 'Treatment T increased Trait Q.', False),
    ('shoot dry matter', 'shoot and root dry matter', 'Both shoot and root dry matter increased.', True),
    ('Trait Q in Organ B', 'Trait Q in Organ A by 12% and in Organ B',
     'Trait Q in Organ A by 12% and in Organ B by 5% increased.', True),
    ('Trait Q in shoots', 'Trait Q in both shoots and roots', 'Trait Q in both shoots and roots increased.', True),
    ('Trait Q in Organ B', 'Treatment T', 'Trait Q in Organ A and in Organ B increased with Treatment T.', False),
    ('Trait Q in Organ B', '', 'Trait Q in Organ A declined. Treatment T increased something in Organ B.', False),
    ('Trait Q in roots', '', 'Trait Q in shoots declined and in roots increased.', False),
    ('shoot dry matter', '', 'shoot and root dry matter rose; shoot and leaf dry matter declined.', False),
    ('root-to-shoot ratio', '', 'The root-to-shoot ratio changed.', True),
])
def test_endpoints_require_valid_derivation_not_just_a_literal_anchor(entity, anchor, source, expected):
    result = endpoint_anchor(entity, anchor, [source])
    assert bool(result) is expected
    if result:
        assert result[0] in source


def test_anchor_gap_is_closed_through_real_extraction_entrypoint(tmp_path, monkeypatch):
    sentence = 'Treatment T increased Trait Q.'
    class Client:
        def chat_json(self, *args, **kwargs):
            answer = {'claims': [{'subject': 'invented mechanism', 'subject_anchor': 'Treatment T',
                'relation': 'increased', 'object': 'Trait Q', 'object_anchor': 'Trait Q',
                'qualifiers': {}, 'evidence_sentence_ids': [1]}]}
            kwargs['response_check'](answer)
            return answer
    pdf = tmp_path / 'source.pdf'
    pdf.write_bytes(b'fixture')
    monkeypatch.setattr('backend.stages.claim_extraction.extractor.source_paper_title', lambda *a: 'Abstract fixture')
    claims, rejected, _ = extract_claims(pdf, Client(), 0, lambda *a: None,
        packets=[packet([row(1, text=sentence)])], source_identifiers={'source_doi': 'fixture'}, bibliography=[])
    assert not claims and rejected['unanchored_evidence'] == 1


def interpretation(identifier, subject='Trait Q', obj='Trait R'):
    bindings = {role: {'iri': 'urn:' + role, 'canonical_iri': 'urn:' + role,
        'mapping': 'exact', 'denotation': 'concept', 'occurrence_id': identifier + role,
        'entity_id': identifier + role} for role in ('subject', 'object', 'relation')}
    return {'candidate_id': identifier, 'document_key': 'document', 'scope': 'in_scope',
        'catalog_signature': 'catalog', 'policy': 'policy', 'claim_signature': identifier,
        'group_signature': identifier, 'bindings': bindings,
        'original_claim': {'subject': subject, 'object': obj, 'relation': 'increases',
            'qualifiers': {}, 'source_evidence_span': 'Same source evidence.'}}


@pytest.mark.parametrize('attribute,value', [('mapping', 'broader'), ('iri', 'urn:different'), ('denotation', 'particular')])
def test_cross_batch_conflicts_are_mapping_holds_not_extraction_holds(attribute, value):
    rows = [interpretation('a'), interpretation('b')]
    rows[1]['bindings']['subject'][attribute] = value
    before = deepcopy(rows[1]['bindings']['subject'])
    flag_consistency(rows)
    assert all(r['mapping_consistency_warnings'] and not r['assembly_eligible'] for r in rows)
    assert all(r['extraction_review_status'] == 'not_flagged' for r in rows)
    assert rows[1]['bindings']['subject']['mapping'] == before['mapping']
    rows[0]['extraction_review'] = {'claim_signature': 'a', 'status': 'accepted'}
    refresh_eligibility(rows[0])
    assert rows[0]['extraction_review_status'] == 'accepted' and not rows[0]['assembly_eligible']
    rows[1]['bindings']['subject'].update({k: rows[0]['bindings']['subject'][k] for k in ('mapping', 'iri', 'denotation')})
    flag_consistency(rows)
    assert all(not r['mapping_consistency_warnings'] and r['assembly_eligible'] for r in rows)
    assert all(r['consistency_policy'] == CONSISTENCY_POLICY for r in rows)


def test_closed_endpoint_agreement_uses_broader_and_restores_original_if_conflict_changes():
    def closed_interpretation(identifier):
        candidate = interpretation(identifier, obj='Outcome ' + identifier)
        candidate.update(policy=ENTITY_GROUNDING_POLICY, mapping_review='model_proposed',
                         property_type_check={'status': 'compatible'})
        candidate['original_claim'].update(source_document_id='doc', source_pdf_sha256='sha',
                                           source_evidence_sentence_numbers=[1], subject_source_mention='Trait Q')
        candidate['bindings']['relation']['fixed_from_extraction'] = True
        return candidate

    first, second = closed_interpretation('a'), closed_interpretation('b')
    first['bindings']['subject'].update(reason='Proposed equivalent meaning.', concept_key='exact:urn:subject',
                                        shared_concept=True)
    second['bindings']['subject']['mapping'] = 'broader'
    rows = [first, second]
    flag_consistency(rows)
    binding = first['bindings']['subject']
    assert binding['mapping'] == 'broader'
    assert binding['entity_id'] == binding['occurrence_id']
    assert not binding['shared_concept'] and binding['concept_key'].startswith('broader:urn:subject:')
    assert binding['consistency_adjustment']['original_reason'] == 'Proposed equivalent meaning.'
    assert binding['consistency_adjustment']['broader_candidate_ids'] == ['b']
    assert not any(row['mapping_consistency_warnings'] for row in rows)
    settled = deepcopy(rows)
    assert flag_consistency(rows) == settled
    before = flag_consistency(deepcopy(rows), reconcile=False)
    assert [row['bindings']['subject']['mapping'] for row in before] == ['exact', 'broader']
    assert all(row['mapping_consistency_warnings'] for row in before)
    assert rows == settled

    third = closed_interpretation('c')
    third['bindings']['subject']['iri'] = 'urn:different'
    flag_consistency([*rows, third])
    assert binding['mapping'] == 'exact'
    assert binding['reason'] == 'Proposed equivalent meaning.'
    assert binding['shared_concept'] and binding['concept_key'] == 'exact:urn:subject'
    assert 'consistency_adjustment' not in binding
    assert all(row['mapping_consistency_warnings'] for row in (*rows, third))

    second['mapping_review'] = 'manually_reviewed'
    flag_consistency(rows)
    assert [row['bindings']['subject']['mapping'] for row in rows] == ['exact', 'broader']
    assert all(row['mapping_consistency_warnings'] for row in rows)


def test_same_species_parent_does_not_collapse_distinct_cultivars_for_relation_comparison():
    rows = [interpretation('a', subject='Cultivar A'), interpretation('b', subject='Cultivar B')]
    for r in rows:
        r['bindings']['subject'].update(iri='urn:species', canonical_iri='urn:species', mapping='broader')
    rows[1]['bindings']['relation']['iri'] = 'urn:different-property'
    flag_consistency(rows)
    assert not any(r['mapping_consistency_warnings'] for r in rows)


@pytest.mark.parametrize('changed', ['document_key', 'qualifiers', 'source_evidence_span', 'source_local_context'])
def test_distinct_contexts_do_not_become_consistency_conflicts(changed):
    rows = [interpretation('a'), interpretation('b')]
    rows[1]['bindings']['subject']['mapping'] = 'broader'
    if changed == 'document_key':
        rows[1][changed] = 'another-document'
    else:
        rows[1]['original_claim'][changed] = {'condition': 'another condition'} if changed == 'qualifiers' else 'Different context.'
    flag_consistency(rows)
    assert not any(r['mapping_consistency_warnings'] for r in rows)


def test_mapping_only_conflict_never_uses_extraction_review_state():
    r = interpretation('a')
    r['mapping_consistency_warnings'] = [{'message': 'Mapping conflict'}]
    refresh_eligibility(r)
    assert review_status(r) == 'not_flagged' and not r['assembly_eligible']
    r['extraction_warnings'] = [{'message': 'Source issue'}]
    assert review_status(r) == 'held'


@pytest.mark.parametrize('field', ['targeting_version', 'reading_context_policy', 'parsing_policy', 'anchoring_policy'])
@pytest.mark.parametrize('location', ['coverage', 'response'])
@pytest.mark.parametrize('missing', [False, True])
def test_cache_requires_matching_input_policies(tmp_path, field, location, missing):
    (tmp_path / 'model_responses').mkdir()
    policies = {'targeting_version': TARGETING_VERSION, 'reading_context_policy': READING_CONTEXT_POLICY,
                'parsing_policy': PARSING_POLICY, 'anchoring_policy': ANCHORING_POLICY,
                'extraction_mode': 'target_context'}
    coverage = {**policies, 'target_id': 'target-abc', 'status': 'completed',
                'output_contract_version': EXTRACTION_CONTRACT_VERSION}
    response = {**policies, 'status': 'ok', 'extraction_contract_version': EXTRACTION_CONTRACT_VERSION,
                'task': 'extraction target 1/1 (target-abc; packet 1)', 'accepted_response': {'claims': []}}
    def write():
        (tmp_path / 'extraction_packets.jsonl').write_text(json.dumps(coverage) + '\n')
        (tmp_path / 'model_responses/a.json').write_text(json.dumps(response))
    write()
    assert _load_cached_packet_claims(tmp_path, 'target_context') == {'target-abc': []}
    target = coverage if location == 'coverage' else response
    if missing:
        target.pop(field)
    else:
        target[field] = 'old'
    write()
    assert _load_cached_packet_claims(tmp_path, 'target_context') == {}


def test_sentence_boundaries_preserve_explicit_paragraphs_without_guessing_missing_punctuation():
    assert page_sentences('First paragraph without punctuation\n\nSecond paragraph without punctuation') == [
        'First paragraph without punctuation', 'Second paragraph without punctuation']
    assert page_sentences('Result was not significant Furthermore another result increased') == [
        'Result was not significant Furthermore another result increased']
    assert page_sentences('Compound Q at 0.5 mg/L changed Trait Q. Trait R increased.') == [
        'Compound Q at 0.5 mg/L changed Trait Q.', 'Trait R increased.']


def test_coordination_never_constructs_a_new_quotation():
    source = 'Trait Q in shoots by 13–25% and in roots by 4–13% increased.'
    for span in coordination_spans(source, 'Trait Q in roots'):
        assert span in source and 'shoots' in span and 'roots' in span


def test_real_provider_archive_retains_policy_metadata_for_cache_reuse(tmp_path, monkeypatch):
    from backend.config.settings import Settings
    from backend.providers.llm import OpenCodeClient
    from backend.stages.claim_extraction.contract import EXTRACTION_RESPONSE_SCHEMA
    from backend.shared.artifacts import save_rows
    folder = tmp_path / 'model_responses'
    client = OpenCodeClient(Settings(), task='extraction', response_dir=folder)
    policies = {'extraction_contract_version': EXTRACTION_CONTRACT_VERSION,
                'targeting_version': TARGETING_VERSION, 'reading_context_policy': READING_CONTEXT_POLICY,
                'parsing_policy': PARSING_POLICY, 'anchoring_policy': ANCHORING_POLICY,
                'extraction_mode': 'target_context'}
    client.set_request_context(policies)
    monkeypatch.setattr(client._inference, 'chat', lambda *args: {
        'content': '{"claims":[]}', 'finish_reason': 'stop', 'usage': {}, 'response_format': 'json_object'})
    client.chat_json('Extract claims.', 'Source sentence.', json_schema=EXTRACTION_RESPONSE_SCHEMA,
                     request_label='extraction target 1/1 (target-abc; packet 1)')
    archives = list(folder.glob('*.json'))
    assert len(archives) == 1
    archive = json.loads(archives[0].read_text(encoding='utf-8'))
    assert all(archive[key] == value for key, value in policies.items())
    save_rows(tmp_path / 'extraction_packets.jsonl', [{**policies, 'output_contract_version': EXTRACTION_CONTRACT_VERSION,
        'target_id': 'target-abc', 'status': 'completed'}])
    assert _load_cached_packet_claims(tmp_path, 'target_context') == {'target-abc': []}


def test_cross_batch_conflicts_survive_interruption_and_resume(tmp_path):
    from backend.ontology.catalog import catalog
    from backend.stages.ontology_grounding.grounder import ground_claims
    from backend.shared.storage import read_jsonl
    from tests.backend.test_ontology_grounding import Grounder, claim
    claims = [claim(key, source_document_id='document', source_packet_number=1,
                    source_evidence_sentence_numbers=[1]) for key in ('a', 'b', 'c')]
    class InterruptingGrounder(Grounder):
        def chat_json(self, *args, **kwargs):
            result = super().chat_json(*args, **kwargs)
            if len(self.calls) == 2:
                result['results'][0]['subject_denotation'] = 'state'
            return result
    reference = catalog()
    with pytest.raises(RuntimeError, match='Simulated interrupted grounding'):
        ground_claims(claims, tmp_path, reference, InterruptingGrounder(fail_at=3), batch_size=1, workers=1)
    saved = read_jsonl(tmp_path / 'ontology_grounding.jsonl')
    assert len(saved) == 2 and all(r['mapping_consistency_warnings'] for r in saved)
    resumed_client = Grounder()
    resumed = ground_claims(claims, tmp_path, reference, resumed_client, batch_size=1, workers=1)
    assert len(resumed_client.calls) == 1 and len(resumed) == 3
    assert all(r['mapping_consistency_warnings'] for r in resumed)
    for r in resumed:
        r['bindings']['subject']['denotation'] = 'concept'
    flag_consistency(resumed)
    assert not any(r['mapping_consistency_warnings'] for r in resumed)


def test_closed_endpoint_reconciles_concept_vs_particular_denotation_and_restores_on_conflict():
    def closed_broader(identifier, denotation):
        candidate = interpretation(identifier, subject='Inoculation with B. subtilis', obj='Outcome ' + identifier)
        candidate.update(policy=ENTITY_GROUNDING_POLICY, mapping_review='model_proposed',
                         property_type_check={'status': 'compatible'})
        candidate['original_claim'].update(source_document_id='doc', source_pdf_sha256='sha',
                                           source_evidence_sentence_numbers=[1], subject_source_mention='Inoculation with B. subtilis')
        candidate['bindings']['subject'].update(mapping='broader', denotation=denotation, reason='Broader exposure.')
        candidate['bindings']['relation']['fixed_from_extraction'] = True
        return candidate

    first, second = closed_broader('a', 'concept'), closed_broader('b', 'particular')
    rows = [first, second]
    flag_consistency(rows)
    binding = second['bindings']['subject']
    assert binding['denotation'] == 'concept'
    assert binding['consistency_adjustment']['original_denotation'] == 'particular'
    assert not any(r['mapping_consistency_warnings'] for r in rows)

    third = closed_broader('c', 'concept')
    third['bindings']['subject']['iri'] = 'urn:different'
    flag_consistency([*rows, third])
    assert binding['denotation'] == 'particular'
    assert 'consistency_adjustment' not in binding
    assert all(r['mapping_consistency_warnings'] for r in (*rows, third))
