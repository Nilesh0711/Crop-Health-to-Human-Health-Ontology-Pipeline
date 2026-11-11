"""Classification-free extraction retains scientific wording and provenance."""
import json
from copy import deepcopy

import pytest

from backend.config.settings import Settings
from backend.config.paths import EXTRACTION_PROMPT
from backend.ontology.catalog import Catalog, RESOURCE_DIR
from backend.providers.llm import OpenCodeClient
from backend.providers.response_repair import validate_schema
from backend.providers.structured_output import SchemaValidationError
from backend.shared.artifacts import save_rows
from backend.stages.claim_extraction.contract import EXTRACTION_RESPONSE_SCHEMA, EXTRACTION_CONTRACT_VERSION, valid_relation_wording
from backend.stages.claim_extraction.deduplication import deduplicate_source_claims
from backend.stages.claim_extraction.extractor import _load_cached_packet_claims
from backend.stages.claim_extraction.prompt import build_extraction_prompt
from backend.stages.ontology_grounding.grounder import request_packet
from backend.stages.ontology_grounding.retrieval import ConceptIndex
from tests.backend.test_extraction_targets import claim, extract_fixture
from tests.backend.test_ontology_grounding import claim as saved_claim


def test_model_output_requests_only_five_required_fields_with_optional_types():
    item = claim([2])
    validate_schema({'claims': [item]}, EXTRACTION_RESPONSE_SCHEMA)
    record = EXTRACTION_RESPONSE_SCHEMA['properties']['claims']['items']
    assert set(record['required']) == {'subject','relation','object','qualifiers','evidence_sentence_ids'}
    assert set(record['properties']) == set(record['required']) | {'subject_type', 'object_type', 'subject_anchor', 'object_anchor'}
    assert 'may' in item['relation']
    assert 'claim_text' not in record['properties']


def test_compact_prompt_has_one_active_vocabulary_and_no_grounding_catalogue():
    ref = Catalog(RESOURCE_DIR)
    prompt = build_extraction_prompt(0, 'target_context') + '\n\n' + ref.extraction_instructions()
    for iri in ref.profile['allowed_classes'] + ref.profile['allowed_object_properties']:
        assert iri not in prompt
    # Extraction is ontology-agnostic: no class or relation catalogue is injected.
    assert 'Approved class names' not in prompt and 'Approved relation names' not in prompt
    assert 'exact_synonyms' not in prompt and 'Common relation names' not in prompt
    assert 'Provisional output labels' not in prompt
    assert len(prompt + json.dumps(EXTRACTION_RESPONSE_SCHEMA)) < 21000


def test_extraction_prompt_uses_rules_without_few_shot_examples():
    prompt = EXTRACTION_PROMPT.read_text(encoding='utf-8')
    assert '<example_source>' not in prompt and '<example_output>' not in prompt
    assert 'FEW-SHOT' not in prompt
    assert 'evidence_sentence_ids' in prompt
    assert 'Never turn A→B→C into A→C' in prompt and 'subject and object must differ' in prompt


def test_hybrid_thinking_defaults_with_explicit_extraction_on_supported():
    from backend.api.contracts import RunOptions
    expected = {task: task == 'pathway_assembly' for task in Settings().task_models}
    assert Settings().task_thinking == expected
    assert RunOptions().thinking.model_dump() == expected
    assert RunOptions(thinking={'extraction': True}).thinking.extraction is True
    assert Settings(thinking={'extraction': True}).task_thinking['extraction'] is True


@pytest.mark.parametrize('task,enabled', [('extraction', False), ('pathway_assembly', True)])
def test_hybrid_defaults_reach_provider_payload_without_network(monkeypatch, task, enabled):
    from backend.providers.inference import InferenceHTTP
    payloads, metrics = [], []
    def receive(self, method, url, **kwargs):
        payloads.append(kwargs['json'])
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}],
                'usage': {'prompt_tokens': 10, 'completion_tokens': 5,
                          'completion_tokens_details': {'reasoning_tokens': 0}}}
    monkeypatch.setattr(InferenceHTTP, '_request', receive)
    client = OpenCodeClient(Settings(), task=task, on_request=metrics.append)
    assert client.chat_json('Return JSON', 'Input') == {'ok': True}
    assert payloads[0]['thinking'] == {'type': 'enabled' if enabled else 'disabled'}
    assert metrics[0]['requested_thinking'] == ('enabled' if enabled else 'disabled')


@pytest.mark.parametrize('relation', ['c', 'd', 't', '', '  ', ' c '])
def test_incomplete_relation_wording_cannot_be_accepted(relation):
    assert not valid_relation_wording(relation)
    item = claim([2]); item['relation'] = relation
    if len(relation) < 2:
        with pytest.raises(SchemaValidationError):
            validate_schema({'claims': [item]}, EXTRACTION_RESPONSE_SCHEMA)


@pytest.mark.parametrize('relation', ['is', 'reduces', 'required for', 'associated_with', 'does not change'])
def test_relation_contract_allows_open_written_predicates_without_decoding_regex(relation):
    item = claim([2]); item['relation'] = relation
    assert valid_relation_wording(relation)
    validate_schema({'claims': [item]}, EXTRACTION_RESPONSE_SCHEMA)
    assert 'pattern' not in EXTRACTION_RESPONSE_SCHEMA['properties']['claims']['items']['properties']['relation']


@pytest.mark.parametrize('bad_relation', ['c', ' c '])
def test_incomplete_predicate_regenerates_from_original_input_before_materialization(tmp_path, monkeypatch, bad_relation):
    valid = {'claims': [claim([2])]}
    invalid = deepcopy(valid); invalid['claims'][0]['relation'] = bad_relation
    calls, metrics = [], []
    client = OpenCodeClient(Settings(), task='extraction', on_request=metrics.append)
    def chat(model, system, user, schema, budget):
        calls.append((model, user))
        return {'content': json.dumps(invalid if len(calls) == 1 else valid),
                'finish_reason': 'stop', 'usage': {}, 'response_format': 'json_schema'}
    monkeypatch.setattr(client._inference, 'chat', chat)
    candidates, rejected, _ = extract_fixture(tmp_path, client, mode='whole_packet')
    assert len(calls) == 2 and calls[0] == calls[1]
    assert candidates[0]['relation'] == 'may contain' and not rejected
    assert metrics[0]['recovery_pending'] and metrics[1]['recovery_completed']


def test_alternate_client_cannot_save_incomplete_relation(tmp_path):
    class Client:
        def chat_json(self, *args, **kwargs):
            item = claim([2]); item['relation'] = 'c'
            return {'claims': [item]}
    candidates, rejected, _ = extract_fixture(tmp_path, Client(), mode='whole_packet')
    assert not candidates and rejected['malformed'] == 1


def test_incomplete_predicates_are_not_reused_as_cached_outputs(tmp_path):
    response_dir = tmp_path / 'model_responses'; response_dir.mkdir()
    save_rows(tmp_path / 'extraction_packets.jsonl', [{'status': 'completed', 'packet_number': 1,
              'output_contract_version': EXTRACTION_CONTRACT_VERSION}])
    item = claim([2]); item['relation'] = ' c '
    (response_dir / 'one.json').write_text(json.dumps({'status': 'ok', 'task': 'extraction packet 1/1',
        'accepted_response': {'claims': [item]}}), encoding='utf-8')
    assert _load_cached_packet_claims(tmp_path, 'whole_packet') == {}


def test_grounding_refuses_saved_incomplete_predicates_before_loading_models(tmp_path):
    from backend.pipelines.ontology import run_grounding
    with pytest.raises(ValueError, match='incomplete relation labels; start a new run'):
        run_grounding(tmp_path, tmp_path, [{'relation': 'c'}], None, Settings(), lambda *args: None)


def test_completed_bad_extraction_cannot_resume(tmp_path):
    from backend.config.settings import SCHEMA_VERSION
    from backend.pipelines.checkpoints import resume_metadata, save_checkpoint
    from backend.shared.storage import file_hash
    pdf = tmp_path / 'sample.pdf'; pdf.write_bytes(b'PDF fixture')
    settings = Settings()
    save_checkpoint(tmp_path, {'schema_version': SCHEMA_VERSION, 'pdf_sha256': file_hash(pdf),
        'task_models': settings.task_models, 'thinking': settings.task_thinking, 'max_pages': 0,
        'claims_per_packet': 0, 'extraction_mode': 'whole_packet', 'extraction_complete': True})
    save_rows(tmp_path / 'candidates.jsonl', [{'candidate_id': 'one', 'subject': 'Material M',
        'relation': 'c', 'object': 'Trait Q', 'schema_error': None, 'source_pdf_sha256': file_hash(pdf)}])
    with pytest.raises(ValueError, match='incomplete relation labels; start a new run'):
        resume_metadata(tmp_path, pdf, settings, 0, 0, extraction_mode='whole_packet')


def test_old_model_classification_fields_require_regeneration_not_local_deletion(monkeypatch):
    valid = {'claims': [claim([2])]}
    invalid = deepcopy(valid)
    invalid['claims'][0]['certainty'] = 'conditional'
    with pytest.raises(SchemaValidationError):
        validate_schema(invalid, EXTRACTION_RESPONSE_SCHEMA)
    calls, metrics = [], []
    client = OpenCodeClient(Settings(), task='extraction', on_request=metrics.append)
    def chat(model, system, user, schema, budget):
        calls.append((model, user))
        return {'content': json.dumps(invalid if len(calls) == 1 else valid),
                'finish_reason': 'stop', 'usage': {}, 'response_format': 'json_object'}
    monkeypatch.setattr(client._inference, 'chat', chat)
    answer = client.chat_json(build_extraction_prompt(0), 'Original numbered scientific text.',
                              json_schema=EXTRACTION_RESPONSE_SCHEMA)
    assert answer == valid and len(calls) == 2 and calls[0] == calls[1]
    assert metrics[0]['recovery_pending'] and metrics[1]['recovery_completed']


def test_cache_requires_current_output_contract_including_empty_results(tmp_path):
    response_dir = tmp_path / 'model_responses'; response_dir.mkdir()
    archive = response_dir / 'one.json'
    archive.write_text(json.dumps({'status': 'ok', 'extraction_contract_version': EXTRACTION_CONTRACT_VERSION, 'task': 'extraction packet 1/1',
                                  'accepted_response': {'claims': []}}), encoding='utf-8')
    assert _load_cached_packet_claims(tmp_path, 'whole_packet') == {}
    save_rows(tmp_path / 'extraction_packets.jsonl', [{'status': 'completed', 'packet_number': 1,
              'output_contract_version': EXTRACTION_CONTRACT_VERSION}])
    assert _load_cached_packet_claims(tmp_path, 'whole_packet') == {'packet_1': []}
    old = claim([2]); old.update(subject_type='Crop', object_type='Contaminant', certainty='conditional')
    archive.write_text(json.dumps({'status': 'ok', 'extraction_contract_version': EXTRACTION_CONTRACT_VERSION, 'task': 'extraction packet 1/1',
                                  'accepted_response': {'claims': [old]}}), encoding='utf-8')
    assert _load_cached_packet_claims(tmp_path, 'whole_packet') == {}


def test_deduplication_keeps_different_source_wording_without_certainty_labels():
    first = saved_claim('one', claim_text='Compound T may affect Process P.')
    for key in ('certainty', 'subject_type', 'object_type'):
        first.pop(key)
    second = {**deepcopy(first), 'candidate_id': 'two', 'claim_text': 'Compound T affects Process P.'}
    third = {**deepcopy(first), 'candidate_id': 'three'}
    rows, duplicates = deduplicate_source_claims([first, second, third])
    assert len(rows) == 2 and duplicates == 1
    assert 'may' in rows[0]['claim_text']
    assert not any('extraction_certainty_variants' in r for r in rows)


def test_grounding_reads_source_wording_instead_of_old_certainty_category():
    source = saved_claim(claim_text='Compound T may affect Process P.')
    source.pop('certainty')
    payload, _ = request_packet([source], ConceptIndex(Catalog(RESOURCE_DIR)))
    assert 'certainty' not in payload['claims']['C1']
    assert 'claim_text' not in payload['claims']['C1']
    assert payload['claims']['C1']['subject'] == source['subject']
    assert payload['claims']['C1']['source_evidence_span'] == source['source_evidence_span']
