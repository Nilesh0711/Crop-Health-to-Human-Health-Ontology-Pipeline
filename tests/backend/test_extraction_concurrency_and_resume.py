import json
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from backend.config.settings import Settings
from backend.providers.inference import InferenceHTTP, RateLimitExhaustedError, InferenceError
from backend.providers.llm import OpenCodeClient, LLMServiceError
from backend.pipelines.checkpoints import resume_available, resume_metadata
from backend.stages.claim_extraction.extractor import (
    _load_cached_packet_claims,
    _iter_packet_extractions,
)


def test_inference_429_retries_once_then_raises_rate_limit_exhausted(monkeypatch):
    settings = Settings()
    inference = InferenceHTTP(settings)

    call_count = 0

    class DummyResponse:
        status_code = 429
        def json(self):
            return {'error': {'message': 'Rate limit reached'}}

    def mock_request(*args, **kwargs):
        nonlocal call_count
        call_count += 1
        return DummyResponse()

    monkeypatch.setattr('httpx.Client.request', mock_request)
    monkeypatch.setattr('time.sleep', lambda s: None)

    with pytest.raises(RateLimitExhaustedError) as exc_info:
        inference._request('POST', 'https://opencode.ai/inference/go/openai/v1/chat/completions')

    assert call_count == 2
    assert '429' in str(exc_info.value)


def test_opencode_client_fallback_to_mimo_on_rate_limit(monkeypatch):
    settings = Settings()
    client = OpenCodeClient(settings, task='extraction', model='opencode-go/deepseek-v4.1-flash')

    attempts = []

    def mock_chat_api_once(system, user, max_tokens, json_schema, request_label, model, response_check, **kwargs):
        attempts.append(model)
        if model == 'opencode-go/deepseek-v4.1-flash':
            raise LLMServiceError('OpenCode inference HTTP 429: Rate or usage limit reached')
        return {'claims': [{'claim_text': 'Valid proposition'}]}

    monkeypatch.setattr(client, '_chat_api_once', mock_chat_api_once)

    result = client.chat_json('System', 'User')

    assert result == {'claims': [{'claim_text': 'Valid proposition'}]}
    assert attempts == ['opencode-go/deepseek-v4.1-flash', 'opencode-go/mimo-v2.6-flash']
    assert client.model == 'opencode-go/mimo-v2.6-flash'


def test_load_cached_packet_claims_and_rehydration():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        responses_dir = tmp_path / 'model_responses'
        responses_dir.mkdir(parents=True)

        resp1 = {
            'status': 'ok',
            'task': 'extraction target 1/2 (target-1111; packet 1)',
            'accepted_response': {
                'claims': [{'claim_text': 'Claim 1'}]
            }
        }
        (responses_dir / 'resp1.json').write_text(json.dumps(resp1), encoding='utf-8')

        resp_failed = {
            'status': 'failed',
            'task': 'extraction target 2/2 (target-2222; packet 2)',
            'error': 'Rate limit'
        }
        (responses_dir / 'resp2.json').write_text(json.dumps(resp_failed), encoding='utf-8')

        cached = _load_cached_packet_claims(tmp_path, 'target_context')
        assert 'target-1111' in cached
        assert len(cached['target-1111']) == 1
        assert cached['target-1111'][0]['claim_text'] == 'Claim 1'
        assert 'target-2222' not in cached


def test_iter_packet_extractions_ordered_yield_with_concurrency():
    packets = [
        {'target_id': 'target-1', 'packet_number': 1, 'section_heading': 'Sec 1', 'sentences': [{'text': 'Sentence 1'}]},
        {'target_id': 'target-2', 'packet_number': 2, 'section_heading': 'Sec 2', 'sentences': [{'text': 'Sentence 2'}]},
        {'target_id': 'target-3', 'packet_number': 3, 'section_heading': 'Sec 3', 'sentences': [{'text': 'Sentence 3'}]},
    ]
    planned_records = [{'target_id': p['target_id'], 'status': 'planned'} for p in packets]

    cached_claims = {
        'target-1': [{'claim_text': 'Cached Claim 1'}],
    }

    client = Mock()
    client.settings.extraction_concurrency = 2

    # Mock _request_packet to return based on target
    def mock_request_packet(pkt, rec, index, total, client, system, title, mode, limit, schema, emit):
        rec['status'] = 'completed'
        return [{'claim_text': f'Live Claim {pkt["target_id"]}'}]

    emitted = []
    with patch('backend.stages.claim_extraction.extractor._request_packet', side_effect=mock_request_packet):
        results = list(_iter_packet_extractions(
            packets=packets,
            planned_records=planned_records,
            client=client,
            system='Sys',
            source_title='Title',
            extraction_mode='target_context',
            claims_per_packet=0,
            response_schema={},
            emit=lambda lvl, msg: emitted.append((lvl, msg)),
            cached_claims=cached_claims,
            concurrency=2
        ))

    assert len(results) == 3
    # Check strict index order 1, 2, 3
    assert results[0][0]['target_id'] == 'target-1'
    assert results[0][2] == [{'claim_text': 'Cached Claim 1'}]

    assert results[1][0]['target_id'] == 'target-2'
    assert results[1][2] == [{'claim_text': 'Live Claim target-2'}]

    assert results[2][0]['target_id'] == 'target-3'
    assert results[2][2] == [{'claim_text': 'Live Claim target-3'}]


def test_checkpoints_partial_extraction_resume():
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp_path = Path(tmpdir)
        checkpoint = {
            'schema_version': 'external-validation-v36',
            'pdf_sha256': 'mock_sha',
            'task_models': Settings().task_models,
            'max_pages': 0,
            'claims_per_packet': 0,
            'extraction_complete': False,
            'extraction_mode': 'target_context',
            'extraction_target_words': 800,
            'targeting_version': 2,
            'thinking': Settings().task_thinking,
        }
        (tmp_path / 'pipeline_checkpoint.json').write_text(json.dumps(checkpoint), encoding='utf-8')

        packets = [
            {'target_id': 'target-1', 'status': 'completed'},
            {'target_id': 'target-2', 'status': 'failed'},
        ]
        with open(tmp_path / 'extraction_packets.jsonl', 'w', encoding='utf-8') as f:
            for pkt in packets:
                f.write(json.dumps(pkt) + '\n')

        # resume_available should be True
        assert resume_available(tmp_path) is True

        # resume_metadata should return partial_extraction
        with patch('backend.pipelines.checkpoints.file_hash', return_value='mock_sha'):
            meta = resume_metadata(tmp_path, Path('fake.pdf'), Settings(), max_pages=0, claims_per_packet=0,
                                   extraction_mode='target_context', target_words=800)
            assert meta.get('partial_extraction') is True
            assert meta.get('completed_targets') == 1
            assert meta.get('total_targets') == 2


def test_extraction_domain_filtering_guards():
    from backend.stages.claim_extraction.extractor import _process_packet_claims, _ExtractionResults, _DocumentEvidence

    state = _ExtractionResults()
    packet = {
        'packet_number': 1,
        'target_id': 'target-1',
        'section_heading': 'Test Section',
        'page_start': 1,
        'page_end': 1,
        'estimated_token_count': 100,
        'sentences': [
            {'sentence_number': 1, 'page': 1, 'text': 'Wheat contains cadmium.'},
            {'sentence_number': 2, 'page': 1, 'text': 'Cadmium affects cadmium directly.'},
            {'sentence_number': 3, 'page': 1, 'text': 'Antibiotics include natural products.'},
            {'sentence_number': 4, 'page': 1, 'text': 'Corporation licenses software tool.'},
        ],
        'target_sentence_numbers': [1, 2, 3, 4],
        'target_source_sentence_ids': ['s1', 's2', 's3', 's4'],
    }
    context = _DocumentEvidence(
        document_id='doc-1',
        digest='digest-1',
        source_title='Test Title',
        source_identifiers={},
        document_scope={'scope': 'in_scope'},
        bibliography=[],
        extraction_mode='target_context'
    )

    items = [
        # 1. Identical endpoints (self-loop)
        {
            'claim_text': 'Cadmium affects cadmium directly.',
            'subject': 'cadmium',
            'subject_source_mention': 'Cadmium',
            'relation': 'affects',
            'relation_trigger': 'affects',
            'object': 'cadmium',
            'object_source_mention': 'cadmium',
            'subject_type': 'Contaminant',
            'object_type': 'Contaminant',
            'certainty': 'causal',
            'evidence_sentence_numbers': [2],
            'qualifiers': {},
        },
        # 2. Dual-Unresolved (out-of-domain)
        {
            'claim_text': 'Antibiotics include natural products.',
            'subject': 'antibiotics',
            'subject_source_mention': 'Antibiotics',
            'relation': 'include',
            'relation_trigger': 'include',
            'object': 'natural products',
            'object_source_mention': 'natural products',
            'subject_type': 'Unresolved',
            'object_type': 'Unresolved',
            'certainty': 'descriptive',
            'evidence_sentence_numbers': [3],
            'qualifiers': {},
        },
        # 3. Non-domain entity types
        {
            'claim_text': 'Corporation licenses software tool.',
            'subject': 'corporation',
            'subject_source_mention': 'Corporation',
            'relation': 'licenses',
            'relation_trigger': 'licenses',
            'object': 'software tool',
            'object_source_mention': 'software tool',
            'subject_type': 'Corporation',
            'object_type': 'Software',
            'certainty': 'descriptive',
            'evidence_sentence_numbers': [4],
            'qualifiers': {},
        },
        # 4. Valid domain-anchored claim (Crop -> Contaminant)
        {
            'claim_text': 'Wheat contains cadmium.',
            'subject': 'wheat',
            'subject_source_mention': 'Wheat',
            'relation': 'contains',
            'relation_trigger': 'contains',
            'object': 'cadmium',
            'object_source_mention': 'cadmium',
            'subject_type': 'Crop',
            'object_type': 'Contaminant',
            'certainty': 'descriptive',
            'evidence_sentence_numbers': [1],
            'qualifiers': {},
        },
    ]

    _process_packet_claims(items, packet, context, state, emit=lambda *args: None)

    assert state.rejected['identical_endpoints'] == 1
    assert state.rejected['out_of_domain_unresolved'] == 1
    assert state.rejected['out_of_domain_entity'] == 1
    assert len(state.candidates) == 1
    assert state.candidates[0]['subject'] == 'wheat'
    assert state.candidates[0]['object'] == 'cadmium'
