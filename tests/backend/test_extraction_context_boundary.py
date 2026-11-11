"""Batch isolation and context budgets without scientific rewriting."""
from copy import deepcopy
import json
from unittest.mock import patch

import pytest

from backend.config.settings import Settings
from backend.providers.llm import OpenCodeClient
from backend.providers.structured_output import SchemaValidationError
from backend.stages.claim_extraction.contract import EXTRACTION_RESPONSE_SCHEMA
from backend.stages.claim_extraction.extractor import (check_extraction_response, _request_packet,
    _proposed_candidate, _bind_claim_evidence, _ExtractionResults)
from backend.stages.claim_extraction.targeting import reading_context_numbers, targeted_prompt, READING_CONTEXT_WORDS
from tests.backend.test_extraction_targets import row, packet


def source_packet():
    source = packet([row(1, text='Exposure E increases Trait Z.'), row(2, text='Treatment T increases Trait Q.')])
    source.update(target_id='target', target_number=1, target_sentence_numbers=[2], target_word_count=5,
                  reading_context_sentence_numbers=[1], reading_context_word_count=5)
    return source


def result():
    return {'claims': [
        {'subject': 'Treatment T', 'relation': 'increases', 'object': 'Trait Q',
         'qualifiers': {'metric': None}, 'evidence_sentence_ids': [2]},
        {'subject': 'Exposure E', 'relation': 'increases', 'object': 'Trait Z',
         'qualifiers': {}, 'evidence_sentence_ids': [1]},
    ]}


def test_context_does_not_expand_to_an_entire_large_paragraph():
    sentences = [row(n, paragraph='long', text=f'Sentence {n} ' + ' '.join(['context'] * 12)) for n in range(1, 101)]
    source = packet(sentences); source['target_sentence_numbers'] = [50]
    selected = reading_context_numbers(source)
    assert sum(len(sentences[n - 1]['text'].split()) for n in selected) <= READING_CONTEXT_WORDS
    assert {49, 51} <= set(selected)
    assert 1 not in selected and 100 not in selected
    assert selected == reading_context_numbers(source)


def test_prompt_distinguishes_support_ids_from_target_findings():
    prompt = targeted_prompt(source_packet(), 'Synthetic study')
    assert 'Target sentence IDs: [2]' in prompt
    assert 'Support-only sentence IDs (never extract separate findings): [1]' in prompt
    assert 'Never append an unrelated target ID' in prompt


def test_unused_null_qualifiers_are_explicit_absence_not_text_repair():
    value = result(); original = deepcopy(value)
    check_extraction_response(value, EXTRACTION_RESPONSE_SCHEMA, [1, 2], [2], enforce_target=False)
    candidate = _proposed_candidate(value['claims'][0])
    assert candidate['qualifiers'] == {}
    assert value == original and value['claims'][0]['qualifiers']['metric'] is None
    for wrong in [12, {}, []]:
        value['claims'][0]['qualifiers']['metric'] = wrong
        with pytest.raises(SchemaValidationError):
            check_extraction_response(value, EXTRACTION_RESPONSE_SCHEMA, [1, 2], enforce_target=False)


def test_mixed_response_keeps_target_claim_and_quarantines_only_context_output():
    source = source_packet(); value = result(); original = deepcopy(value)
    check_extraction_response(value, EXTRACTION_RESPONSE_SCHEMA, [1, 2], [2], enforce_target=False)
    with pytest.raises(SchemaValidationError, match=r'target finding IDs=\[2\].*support-only IDs=\[1\]'):
        check_extraction_response(value, EXTRACTION_RESPONSE_SCHEMA, [1, 2], [2])
    state = _ExtractionResults()
    accepted = [_bind_claim_evidence(item, _proposed_candidate(item), source, 'target_context', state) for item in value['claims']]
    assert accepted[0] is not None and accepted[1] is None
    assert state.rejected == {'context_only_finding': 1}
    assert state.rejection_records[0]['source_evidence_sentence_numbers'] == [1]
    assert state.rejection_records[0]['proposed_claim']['subject'] == 'Exposure E'
    assert value == original


def test_provider_does_not_regenerate_valid_batch_for_null_or_context_only_rows():
    source = source_packet(); metrics = []; requests = []; events = []
    client = OpenCodeClient(Settings(), task='extraction', on_request=metrics.append)
    def chat(*args):
        requests.append(args)
        return {'content': json.dumps(result()), 'finish_reason': 'stop', 'usage': {}, 'response_format': 'json_object'}
    record = {}
    with patch.object(client._inference, 'chat', side_effect=chat):
        claims = _request_packet(source, record, 1, 1, client, 'Source rules.', 'Synthetic study',
                                 'target_context', 0, EXTRACTION_RESPONSE_SCHEMA, lambda *args: events.append(args))
    assert len(requests) == 1 and [m['status'] for m in metrics] == ['ok']
    assert len(claims) == 2 and record['status'] == 'completed'
    assert record['context_only_claims_quarantined'] == 1
    assert any('quarantined' in message for _, message in events)


def test_unknown_sentence_ids_still_require_structural_recovery():
    value = result(); value['claims'][0]['evidence_sentence_ids'] = [99]
    with pytest.raises(SchemaValidationError, match='allowed input IDs'):
        check_extraction_response(value, EXTRACTION_RESPONSE_SCHEMA, [1, 2], [2], enforce_target=False)
