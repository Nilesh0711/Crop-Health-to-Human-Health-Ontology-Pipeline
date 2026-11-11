"""Target ownership, reading context and bounded recovery are scientific safeguards."""
from copy import deepcopy
from unittest.mock import patch

import pytest

from backend.config.settings import Settings
from backend.providers.llm import OpenCodeClient, LLMServiceError
from backend.providers.structured_output import IncompleteResponseError, SchemaValidationError
from backend.stages.claim_extraction.extractor import extract_claims
from backend.stages.claim_extraction.prompt import build_extraction_prompt
from backend.stages.claim_extraction.targeting import (
    build_extraction_targets, check_target_evidence, sentence_identity, targeted_prompt, reading_context_numbers,
)
from backend.stages.claim_extraction.contract import EXTRACTION_RESPONSE_SCHEMA
from backend.stages.claim_extraction.extractor import check_extraction_response


def row(number, *, paragraph=None, heading='Results', chapter='chapter-1', text=None):
    return dict(page=1, page_sentence_index=number, paragraph_id=paragraph or f'p-{number}',
                section_heading=heading, chapter_id=chapter,
                text=text or f'Sentence {number} reports a measured experimental result in soil.')


def packet(rows, number=1):
    words = sum(len(s['text'].split()) for s in rows)
    return dict(packet_number=number, section_heading='Results', chapter_id=rows[0]['chapter_id'],
                page_start=1, page_end=1, word_count=words, estimated_token_count=round(words * 1.4),
                sentences=rows)


def test_every_occurrence_is_targeted_once_even_with_packet_overlap_and_no_domain_keywords():
    rows = [row(i, text=f'This neutral discussion sentence {i} has no required pathway keyword.') for i in range(1, 10)]
    packets = [packet(rows[:6]), packet(rows[4:], 2)]
    original = deepcopy(packets)
    targets = build_extraction_targets(packets, target_words=25)
    received = [identifier for t in targets for identifier in t['target_source_sentence_ids']]
    expected = [sentence_identity(s, packets[0]) for s in rows]
    assert received == expected
    assert len(received) == len(set(received))
    assert packets == original
    assert targets == build_extraction_targets(packets, target_words=25)
    assert all(t['sentences'] == packets[t['packet_number'] - 1]['sentences'] for t in targets)


def test_targets_prefer_paragraphs_and_do_not_cross_chapters():
    rows = [row(1, paragraph='a'), row(2, paragraph='a'), row(3, paragraph='b'),
            row(4, paragraph='c', heading='Discussion'), row(5, chapter='chapter-2')]
    targets = build_extraction_targets([packet(rows)], target_words=25)
    assert targets[0]['target_sentence_numbers'] == [1, 2]
    assert targets[1]['target_sentence_numbers'] == [3, 4]
    for t in targets:
        selected = [t['sentences'][n - 1] for n in t['target_sentence_numbers']]
        assert len({s['chapter_id'] for s in selected}) == 1
    assert [n for t in targets for n in t['target_sentence_numbers']] == list(range(1, 6))


def test_oversized_paragraph_splits_at_sentences_and_single_long_sentence_remains_intact():
    rows = [row(1, paragraph='long'), row(2, paragraph='long'),
            row(3, text=' '.join(['scientific'] * 50))]
    targets = build_extraction_targets([packet(rows)], target_words=10)
    assert [t['target_sentence_numbers'] for t in targets] == [[1], [2], [3]]
    assert targets[-1]['target_word_count'] == 50


@pytest.mark.parametrize('numbers', [[1], [], [True], ['2'], [3, 2], [2, 2], [2, 3, 4, 5]])
def test_context_only_or_invalid_evidence_ids_are_rejected(numbers):
    with pytest.raises(SchemaValidationError, match='supplied sentence IDs'):
        check_target_evidence({'claims': [{'evidence_sentence_numbers': numbers}]}, [2, 3, 4])


def test_empty_output_and_consecutive_target_ids_are_valid():
    check_target_evidence({'claims': []}, [2, 3])
    check_target_evidence({'claims': [{'evidence_sentence_numbers': [2, 3]}]}, [2, 3])


def test_target_prompt_preserves_numbering_context_and_excludes_global_priority_competition():
    target = build_extraction_targets([packet([row(1), row(2), row(3)])], target_words=10)[1]
    prompt = targeted_prompt(target, 'Original paper')
    core = prompt.split('<extraction_target>')[1].split('</extraction_target>')[0]
    context = prompt.split('<reading_context_only>')[1].split('</reading_context_only>')[0]
    assert '[2]' in core and '[1]' not in core and '[3]' not in core
    assert '[1]' in context and '[3]' in context and '[2]' not in context
    system = build_extraction_prompt(4, 'target_context')
    assert 'Start with the priority evidence index' not in system
    assert 'Extract only from extraction_target' in system
    assert 'Use the priority evidence index as an attention aid' in build_extraction_prompt(4, 'whole_packet')


def test_local_context_keeps_qualifiers_and_antecedents_but_omits_remote_findings():
    rows = [row(n, text=f'Remote paragraph {n} describes unrelated observations.') for n in range(1, 10)]
    rows[3] = row(4, text='The treatment lasted seven days in nutrient solution.')
    rows[4] = row(5, text='The plants received Treatment T at the reported dose.')
    rows[5] = row(6, text='These plants showed changes in root length.')
    target = packet(rows)
    target.update(target_id='fixture', target_sentence_numbers=[6])
    assert reading_context_numbers(target) == [4, 5, 7, 8]
    original = deepcopy(target)
    reduced = targeted_prompt(target, 'Source')
    full = targeted_prompt(target, 'Source', context_mode='full')
    assert 'seven days in nutrient solution' in reduced
    assert '[5] [page 1] The plants received Treatment T' in reduced
    assert 'Remote paragraph 1' not in reduced and 'Remote paragraph 1' in full
    assert target == original
    assert reduced.split('<extraction_target>')[1].split('</extraction_target>')[0] == full.split('<extraction_target>')[1].split('</extraction_target>')[0]


def test_context_keeps_whole_boundary_paragraph_and_never_crosses_chapters():
    rows = [row(1, chapter='previous'), row(2, paragraph='same'), row(3, paragraph='same'),
            row(4, paragraph='same'), row(5, chapter='next')]
    target = packet(rows)
    target.update(target_id='fixture', target_sentence_numbers=[3])
    assert reading_context_numbers(target) == [2, 4]
    assert target['sentences'][1]['text'] in targeted_prompt(target, 'Source')


def test_large_context_paragraph_is_bounded_without_cutting_sentences_or_skipping_to_remote_text():
    rows = [row(1, paragraph='remote'), row(2, paragraph='large', text=' '.join(['context'] * 700)),
            row(3, paragraph='large', text='The final condition belongs to the preceding sentence.'),
            row(4, paragraph='target'), row(5, paragraph='after')]
    target = packet(rows)
    target.update(target_id='fixture', target_sentence_numbers=[4])
    assert reading_context_numbers(target) == [3, 5]


def test_remote_abbreviation_definitions_remain_source_bound_without_remote_findings():
    rows = [row(1, text='Dry matter (DM) was recorded. Compound Z has another effect.')]
    rows += [row(n) for n in range(2, 9)]
    rows[6] = row(7, text='Treatment T affected DM in the plants.')
    target = next(t for t in build_extraction_targets([packet(rows)], target_words=10)
                  if 7 in t['target_sentence_numbers'])
    prompt = targeted_prompt(target, 'Source')
    assert 'Dry matter (DM)' in prompt
    assert 'Compound Z has another effect' not in prompt
    assert 'source_definitions_only' in prompt
    assert target['source_abbreviation_definitions'][0]['page'] == 1


@pytest.mark.parametrize('field,value', [('subject', ''), ('object', ''), ('subject', '  '), ('relation', '  ')])
def test_blank_endpoints_and_predicates_are_schema_errors_not_truncation(field, value):
    item = claim([2]); item[field] = value
    with pytest.raises(SchemaValidationError):
        check_extraction_response({'claims': [item]}, EXTRACTION_RESPONSE_SCHEMA, [2])


def test_empty_endpoint_recovery_preserves_source_and_reports_schema_error():
    import json
    from tests.external.test_generation_budget import response
    bad = claim([2]); bad['object'] = ''
    good = claim([2])
    metrics = []
    client = OpenCodeClient(Settings(), task='extraction', on_request=metrics.append)
    with patch.object(client._inference, 'chat', side_effect=[response(json.dumps({'claims': [bad]})),
            response(json.dumps({'claims': [good]}))]) as call:
        answer = client.chat_json('Source rules', 'Original source text', json_schema=EXTRACTION_RESPONSE_SCHEMA,
            response_check=lambda result: check_extraction_response(result, EXTRACTION_RESPONSE_SCHEMA, [2]))
    assert answer == {'claims': [good]}
    assert metrics[0]['failure_category'] == 'schema_error'
    assert all(c.args[2] == 'Original source text' for c in call.call_args_list)
    assert [c.args[4] for c in call.call_args_list] == [50000, 50000]


def test_resume_rejects_old_context_policy_without_modifying_its_checkpoint(tmp_path):
    import json
    from pathlib import Path
    from backend.config.settings import SCHEMA_VERSION
    from backend.pipelines.checkpoints import resume_metadata
    settings = Settings()
    checkpoint = {'schema_version': SCHEMA_VERSION, 'pdf_sha256': 'fixture-hash',
        'task_models': settings.task_models, 'max_pages': 0, 'claims_per_packet': 0,
        'extraction_mode': 'target_context', 'extraction_target_words': 800,
        'targeting_version': 2, 'thinking': settings.task_thinking}
    path = tmp_path / 'pipeline_checkpoint.json'
    path.write_text(json.dumps(checkpoint), encoding='utf-8')
    before = path.read_bytes()
    with patch('backend.pipelines.checkpoints.file_hash', return_value='fixture-hash'):
        with pytest.raises(ValueError, match='targeting version changed'):
            resume_metadata(tmp_path, Path('fixture.pdf'), settings, 0, 0, extraction_mode='target_context', target_words=800)
    assert path.read_bytes() == before


def claim(numbers):
    return dict(subject='Wheat grains', relation='may contain', object='cadmium',
                evidence_sentence_ids=numbers,
                qualifiers=dict(crop_or_species='wheat', condition='disease Z infection',
                                population='', comparator='', metric='cadmium presence'))


def extract_fixture(tmp_path, client, *, mode, records=None):
    pdf = tmp_path / 'fixture.pdf'
    pdf.write_bytes(b'PDF text is supplied by the parsed fixture')
    rows = [row(1, text='This section describes wheat grains under disease Z infection.'),
            row(2, text='Wheat grains may contain cadmium under these conditions.')]
    with patch('backend.stages.claim_extraction.extractor.source_paper_title', return_value='Fixture'):
        return extract_claims(pdf, client, 0, lambda *args: None, packets=[packet(rows)],
                              source_identifiers={'source_doi': 'fixture'}, bibliography=[],
                              extraction_mode=mode, target_words=10, packet_results=records)


def test_direct_extraction_retains_context_qualifiers_provenance_and_original_packet_ids(tmp_path):
    class Client:
        calls = []

        def chat_json(self, system, user, **kwargs):
            self.calls.append((system, user))
            target = user.split('<extraction_target>')[1].split('</extraction_target>')[0]
            value = {'claims': [claim([2])]} if '[2]' in target else {'claims': []}
            kwargs['response_check'](value)
            return value

    client, records = Client(), []
    candidates, rejected, _ = extract_fixture(tmp_path, client, mode='target_context', records=records)
    assert len(client.calls) == 2 and len(candidates) == 1
    c = candidates[0]
    assert c['qualifiers']['condition'] == 'disease Z infection'
    assert 'may' in c['relation'] and c['schema_error'] is None
    assert 'claim_text' not in c
    assert not {'certainty', 'subject_type', 'object_type'} & c.keys()
    assert c['source_evidence_sentence_numbers'] == [2] and c['source_packet_number'] == 1
    assert c['source_evidence_span'] == 'Wheat grains may contain cadmium under these conditions.'
    assert 'disease Z infection' in c['source_local_context']
    assert c['source_target_id'] == records[1]['target_id']
    assert c['source_pdf_sha256'] and not rejected
    assert all(r['status'] == 'completed' for r in records)


def test_alternate_client_cannot_bypass_target_ownership_by_skipping_response_callback(tmp_path):
    class Client:
        def chat_json(self, *args, **kwargs):
            return {'claims': [claim([2])]}

    candidates, rejected, _ = extract_fixture(tmp_path, Client(), mode='target_context')
    assert len(candidates) == 1 and rejected['context_only_finding'] == 1


def test_whole_packet_keeps_single_call_and_priority_index(tmp_path):
    class Client:
        calls = []

        def chat_json(self, system, user, **kwargs):
            self.calls.append(user)
            return {'claims': [claim([2])]}

    client = Client()
    candidates, _, _ = extract_fixture(tmp_path, client, mode='whole_packet')
    assert len(client.calls) == 1 and len(candidates) == 1
    assert 'PRIORITY EVIDENCE INDEX' in client.calls[0]
    assert '<extraction_target>' not in client.calls[0]


def test_primary_failure_marks_later_targets_not_started_instead_of_complete(tmp_path):
    class Client:
        def chat_json(self, *args, **kwargs):
            raise LLMServiceError('Primary provider timeout')

    records = []
    with pytest.raises(LLMServiceError):
        extract_fixture(tmp_path, Client(), mode='target_context', records=records)
    assert [r['status'] for r in records] == ['service_error', 'not_started']


def test_real_response_recovery_keeps_target_guard_original_evidence_and_selected_model():
    from tests.external.test_generation_budget import response
    import json

    client = OpenCodeClient(Settings(), task='extraction')
    outside = json.dumps({'claims': [{'evidence_sentence_numbers': [80]}]})
    inside = json.dumps({'claims': [{'evidence_sentence_numbers': [2]}]})
    schema = {'type': 'object', 'properties': {'claims': {'type': 'array'}}, 'required': ['claims']}
    with patch.object(client._inference, 'chat', side_effect=[response(outside), response(inside)]) as call:
        answer = client.chat_json('Target rules', 'Exact original evidence', json_schema=schema,
                                  response_check=lambda value: check_target_evidence(value, [2, 3]))
    assert answer['claims'][0]['evidence_sentence_numbers'] == [2]
    assert call.call_count == 2
    assert [c.args[4] for c in call.call_args_list] == [50000, 50000]
    assert all(c.args[0] == Settings().extraction_model for c in call.call_args_list)
    assert all(c.args[2] == 'Exact original evidence' for c in call.call_args_list)


def test_uncapped_extraction_returns_more_than_four_claims_with_exact_source_passage(tmp_path):
    text = 'Wheat grains may contain toxin A, toxin B, toxin C, toxin D and toxin E under disease Z infection.'
    class Client:
        schema = None
        def chat_json(self, system, user, **kwargs):
            self.schema = kwargs['json_schema']
            assert 'no numeric claim-count cap' in system
            rows = []
            for name in ('toxin A', 'toxin B', 'toxin C', 'toxin D', 'toxin E'):
                item = claim([1])
                item.update(object=name)
                rows.append(item)
            value = {'claims':rows}
            kwargs['response_check'](value)
            return value
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'fixture')
    client = Client()
    records = []
    with patch('backend.stages.claim_extraction.extractor.source_paper_title', return_value='Fixture'):
        candidates, _, _ = extract_claims(pdf, client, 0, lambda *args:None,
            packets=[packet([row(1, text=text)])], source_identifiers={'source_doi':'fixture'}, bibliography=[],
            extraction_mode='target_context', claims_per_packet=0, packet_results=records)
    assert len(candidates) == 5
    assert 'maxItems' not in client.schema['properties']['claims']
    assert all(c['source_evidence_span'] == text for c in candidates)
    assert 'claim_text' not in candidates[0]
    assert not records[0]['claim_limit_reached']


def test_anaphoric_passage_yields_both_direct_edges_without_a_transitive_shortcut(tmp_path):
    rows = [row(1, text='Fusarium head blight affects wheat grains.'),
            row(2, text='These infected grains may contain mycotoxins.')]
    first = claim([1])
    first.update(subject='Fusarium head blight', object='wheat grains', relation='affects',
                 qualifiers={k:'' for k in first['qualifiers']})
    second = claim([1,2])
    second.update(subject='These infected grains', object='mycotoxins',
                  qualifiers={**second['qualifiers'], 'condition':'Fusarium head blight infection'})
    class Client:
        def chat_json(self, system, user, **kwargs):
            value = {'claims':[first,second]}
            kwargs['response_check'](value)
            return value
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'fixture')
    with patch('backend.stages.claim_extraction.extractor.source_paper_title', return_value='Fixture'):
        candidates, _, _ = extract_claims(pdf, Client(), 0, lambda *args:None, packets=[packet(rows)],
            source_identifiers={'source_doi':'fixture'}, bibliography=[], extraction_mode='target_context')
    assert len(candidates) == 2
    assert candidates[0]['object'] == 'wheat grains'
    assert candidates[1]['subject'] == 'These infected grains'
    assert candidates[1]['subject_source_mention'] == 'These infected grains'
    assert 'may contain' in candidates[1]['relation'] and 'certainty' not in candidates[1]
    assert 'claim_text' not in candidates[1]
    assert 'Fusarium head blight' in candidates[1]['source_reading_context']
    assert candidates[1]['qualifiers']['condition'] == 'Fusarium head blight infection'
