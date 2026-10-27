"""Observed v25 failure modes: skipped packets and provenance poisoning."""

import copy
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.domain.claim_context import apply_source_provenance
from backend.stages.claim_extraction.prompt import build_extraction_prompt
from backend.stages.claim_extraction.extractor import (
    extract_claims,
    extraction_priority_sentence_numbers,
    extraction_review_hints,
)
from backend.config.settings import Settings
from backend.stages.external_validation.proofs import _derive_evidence_quotes
from tests.external.helpers import judge_document
from backend.providers.llm import LLMError, OpenCodeClient
from backend.stages.literature_retrieval.workflow import _bibliographic_key, _independence_group
from backend.stages.query_planning.planner import evidence_query_plan
from tests.regression.test_pipeline_v24 import ReplyClient, external_fixture


def occurrence():
    return {'subject': 'crop R', 'object': 'compound T', 'relation': 'has_contaminant',
            'subject_type': 'Crop', 'object_type': 'Contaminant', 'certainty': 'descriptive',
            'qualifiers': {'condition': 'commercial farming in region S'},
            'source_evidence_span': 'Crop R samples from commercial farming in region S contained compound T.'}


def provenance_vote():
    return {'decision': 'entailed', 'occurrence_provenance_fields': [{
        'field': 'condition', 'value': 'commercial farming in region S',
        'evidence': 'commercial farming in region S', 'rationale': 'Source sampling setting, not a biological restriction.'}]}


def test_provenance_is_model_classified_grounded_preserved_and_not_an_external_filter():
    candidate = occurrence()
    changes = apply_source_provenance(candidate, provenance_vote())
    assert len(changes) == 1 and candidate['qualifiers']['condition'] == ''
    assert candidate['source_context'] == 'commercial farming in region S'
    assert changes[0]['moved_value'] == 'commercial farming in region S'
    plan = evidence_query_plan(candidate, {'query': 'crop R AND compound T', 'openalex_query': 'crop R compound T'})
    assert all('region S' not in query for queries in plan.values() for query in queries)


def test_missing_or_ungrounded_role_votes_cannot_erase_conditions():
    for vote in ({'decision': 'entailed'}, {'decision': 'needs_review', **{'occurrence_provenance_fields': provenance_vote()['occurrence_provenance_fields']}},
                 {**provenance_vote(), 'occurrence_provenance_fields': [{**provenance_vote()['occurrence_provenance_fields'][0], 'evidence': 'Invented setting'}]}):
        candidate = occurrence()
        assert not apply_source_provenance(candidate, vote)
        assert candidate['qualifiers']['condition'] == 'commercial farming in region S'
    for update in ({'certainty': 'conditional'}, {'relation': 'increases'}):
        candidate = {**occurrence(), **update}
        assert not apply_source_provenance(candidate, provenance_vote())


def test_unclassified_infection_and_dose_conditions_remain_restrictions():
    candidate = occurrence()
    candidate['qualifiers']['condition'] = 'infected grains after treatment at dose D'
    assert not apply_source_provenance(candidate, {'decision': 'entailed', 'occurrence_provenance_fields': []})
    assert candidate['qualifiers']['condition'] == 'infected grains after treatment at dose D'


def test_timeout_and_invalid_output_are_failed_coverage_not_zero_claim_evidence():
    packet = {'packet_number': 1, 'section_heading': 'Local section', 'page_start': 1, 'page_end': 1,
              'word_count': 12, 'estimated_token_count': 17, 'sentences': [{'page': 1, 'section_heading': 'Local section',
                'text': 'Crop R samples contained compound T in the study.'}]}
    for response, expected in (({'ok': True}, 'failed'), ({'claims': []}, 'completed'), (None, 'timeout')):
        client = ReplyClient(response)
        if response is None:
            client.chat_json = lambda *a, **kw: (_ for _ in ()).throw(LLMError('Model timed out after 300s'))
        records = []
        with TemporaryDirectory() as temporary:
            pdf = Path(temporary) / 'paper.pdf'
            pdf.write_bytes(b'PDF parser mocked')
            with patch('backend.stages.claim_extraction.extractor.source_paper_title', return_value='Paper'), patch('backend.stages.claim_extraction.extractor.pdf_bibliography', return_value={}):
                claims, _, _ = extract_claims(pdf, client, 0, lambda *_: None, packets=[packet],
                                             source_identifiers={'source_doi': 'fixture'}, packet_results=records)
        assert not claims and records[0]['status'] == expected


def test_extraction_prompt_is_the_editable_template_and_has_explicit_domain_priorities():
    prompt = build_extraction_prompt(4)
    assert 'Restrictions versus provenance' in prompt and 'Return at most 4' in prompt
    assert 'Animal toxicity is not a HumanHealthOutcome' in prompt
    assert 'harvest tonnage/productivity alone' in prompt
    assert 'source_context' in prompt
    sentences = [{'text': 'A plant disease reduces yields and affects crop production and lowers crop productivity.'},
                 {'text': 'Contaminant Q may be produced in infected crop grains.'},
                 {'text': 'These examples were chosen to examine nutrition, contamination and impacts on crops.'}]
    ranked = extraction_priority_sentence_numbers(sentences)
    assert 2 in ranked and 3 in ranked  # No phrase-specific editorial blacklist.
    assert ranked.index(2) < ranked.index(1)  # Domain attention remains non-blocking.


def test_production_and_containment_require_evidence_assessment_not_type_rules():
    shape = {'subject': 'disease D', 'subject_type': 'PlantDisease', 'relation': 'has_contaminant',
             'object': 'compound T', 'object_type': 'Contaminant', 'qualifiers': {}}
    assert 'review_composition_subject_role' in extraction_review_hints(shape, 'The pathogen produces compound T.')




def test_sparse_sentence_ids_only_fill_a_short_actual_consecutive_interval():
    document = {'evidence_chunks': [{'chunk_id': 'one', 'text': 'First statement. Second statement. Third statement. Fourth statement.'}]}
    decision = {'evidence': [{'chunk_id': 'one', 'sentence_numbers': [1, 3]}]}
    assert not _derive_evidence_quotes(document, decision)
    assert decision['evidence'][0]['quoted_span'] == 'First statement. Second statement. Third statement.'
    for ids in ([1, 4], [1, 1000000000], [1, 1], [False]):
        assert _derive_evidence_quotes(document, {'evidence': [{'chunk_id': 'one', 'sentence_numbers': ids}]})


def test_interval_completion_does_not_bypass_the_contradiction_check():
    candidate, document, reply = external_fixture()
    document['evidence_chunks'][0]['text'] += ' The other measured outcome did not increase. These were separate measurements.'
    reply['evidence'][0]['sentence_numbers'] = [1, 3]

    class NLI:
        settings = Settings()
        def score(self, premise, hypothesis):
            assert 'did not increase' in premise
            return {'entailment': 0.01, 'neutral': 0.01, 'contradiction': 0.98}
    assert judge_document(ReplyClient(reply), candidate, document, nli_validator=NLI())['decision'] != 'supported'


def test_identical_bibliography_with_conflicting_dois_is_not_independent_support():
    first = {'title': 'Detailed analysis of compound T in crop R samples', 'author_string': 'Author A, Author B',
             'publication_year': '2020', 'doi': '10.example/record-one'}
    second = {**first, 'doi': '10.example/record-two'}
    assert _bibliographic_key(first) == _bibliographic_key(second)
    assert _independence_group(first) == _independence_group(second)
    assert _independence_group({**first, 'publication_year': '2021'}) != _independence_group(first)


def test_legacy_completed_label_is_annotated_without_rewriting_saved_summary():
    from backend.api.app import _summary_with_coverage
    from backend.shared.storage import write_jsonl
    with TemporaryDirectory() as temporary:
        output = Path(temporary)
        original = '{"run_status":"completed","extracted":4}'
        (output / 'summary.json').write_text(original, encoding='utf-8')
        write_jsonl(output / 'llm_requests.jsonl', [
            {'task': 'extraction packet 1/3', 'status': 'timeout'},
            {'task': 'extraction packet 2/3', 'status': 'timeout'},
            {'task': 'extraction packet 3/3', 'status': 'ok'}])
        summary = _summary_with_coverage(output)
        assert summary['extraction_coverage']['complete'] is False
        assert summary['extraction_coverage']['completed_packets'] == 1
        assert len(summary['extraction_coverage']['failed_packets']) == 2
        assert (output / 'summary.json').read_text(encoding='utf-8') == original
