"""Chapter provenance, lossless evidence packing, citation lifecycle and progress."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from backend.providers.inference import InferenceError
from unittest.mock import Mock, patch

import pytest

from backend.stages.pdf_parsing.structure import inspect_pdf
from backend.stages.pdf_parsing.packets import iter_pdf_context_packets, pdf_bibliography, link_source_citations
from backend.config.settings import Settings
from backend.stages.external_validation.proofs import _numbered_chunks
from backend.stages.external_validation.joint import packed_document, judge_evidence_pack
from backend.providers.llm import OpenCodeClient, LLMServiceError
from backend.stages.literature_retrieval.workflow import _citation_results, citation_tracking
from backend.stages.external_validation.workflow import validate_candidate
from backend.stages.query_planning.planner import evidence_query_plan
from tests.external.test_joint_validation import fixture


def reader_for(texts, bookmarks=()):
    destinations = [SimpleNamespace(title=title, page=page) for title, page in bookmarks]
    return SimpleNamespace(pages=[SimpleNamespace(extract_text=lambda text=text, **kw: text) for text in texts],
                           outline=destinations, get_destination_page_number=lambda d: d.page - 1)


def test_chapter_bibliographies_do_not_end_document_or_cross_link_reused_numbers(tmp_path):
    reader = reader_for([
        'Chapter 1\nAbstract\nCrop A contains toxin B in this controlled experiment [1].\nReferences\n1. Smith. First paper. 2020. 10.1234/first',
        '2. Jones. Continuation of bibliography. 2021.',
        'Chapter 2\nAbstract\nLead and cadmium reduced microbial activity in soil [1].\nReferences\n1. Khan. Second paper. 2010. 10.1234/second',
        '2. White. Final bibliography entry. 2022.',
    ])
    with patch('backend.stages.pdf_parsing.packets.PdfReader', return_value=reader):
        packets = list(iter_pdf_context_packets(tmp_path / 'book.pdf'))
        bibliography = pdf_bibliography(tmp_path / 'book.pdf')
    text = ' '.join(s['text'] for p in packets for s in p['sentences'])
    assert 'Lead and cadmium reduced microbial activity' in text
    assert 'First paper' not in text and 'Final bibliography' not in text
    assert 1 not in bibliography  # Duplicate global key is deliberately unavailable.
    for page, chapter, doi in [(1, 'chapter-1', '10.1234/first'), (3, 'chapter-3', '10.1234/second')]:
        references, links = link_source_citations(bibliography, 'Finding [1].', page, chapter)
        assert references[0]['doi'] == doi
        assert links[0]['status'] == 'identified'
    assert all(len({s['chapter_id'] for s in p['sentences']}) == 1 for p in packets)


def test_article_section_bookmarks_do_not_separate_its_bibliography(tmp_path):
    reader = reader_for(['Abstract\nCrop A contains toxin B [1].', 'Results\nThe measured concentration increased.',
                         'References\n1. Smith. Single article bibliography. 2020.'],
                        [('Introduction', 1), ('Results', 2), ('References', 3)])
    with patch('backend.stages.pdf_parsing.packets.PdfReader', return_value=reader):
        bibliography = pdf_bibliography(tmp_path / 'article.pdf')
    references, _ = link_source_citations(bibliography, 'Finding [1].', 1, 'chapter-1')
    assert len(references) == 1


def test_missing_author_year_link_and_wrong_chapter_are_visible():
    wrong = {'reference_number': 1, 'citation_text': 'Wrong chapter', 'source_page_start': 20, 'source_page_end': 30}
    references, links = link_source_citations({1: wrong}, 'Lead reduced growth (Baloch et al., 2024) [1].', 3, 'chapter-1')
    assert not references
    assert {l['citation_key'] for l in links} == {'1', 'baloch|2024'}
    assert all(l['status'] == 'missing_bibliography_link' for l in links)
    tracked = citation_tracking({'source_citation_links': links})
    assert len(tracked) == 2 and all(not row['resolved'] for row in tracked)


def test_ambiguous_reference_key_is_reported_instead_of_selecting_first_entry(tmp_path):
    reader = reader_for(['Abstract\nA scientific finding [1].\nReferences\n1. Smith. First study. 2020.\n1. Jones. Different study. 2021.'])
    with patch('backend.stages.pdf_parsing.packets.PdfReader', return_value=reader):
        bibliography = pdf_bibliography(tmp_path / 'ambiguous.pdf')
    references, links = link_source_citations(bibliography, 'A finding [1].', 1, 'chapter-1')
    assert not references and links[0]['status'] == 'missing_bibliography_link'


def test_page_audit_reports_missing_text_errors_and_unverified_order():
    reader = reader_for(['A readable scientific sentence describes the measured effect of soil treatment on crop growth.', '', 'a b c d e f g h i j k l m n o'])
    def fail(**kw):
        raise ValueError('Unreadable page')
    reader.pages.append(SimpleNamespace(extract_text=fail))
    audit = inspect_pdf(reader, max_pages=2)
    assert audit['total_pages'] == 4 and audit['selected_pages'] == 2
    assert audit['pages'][1]['flags'] == ['no_extracted_text_ocr_or_blank_page']
    assert audit['pages'][0]['reading_order'] == 'not_verified'
    assert audit['pages'][2]['reading_order'] == 'suspect'
    assert not audit['pages'][2]['within_page_limit']
    assert 'extraction_error:ValueError' in audit['pages'][3]['flags']


def test_blank_pdf_persists_page_diagnostics_before_stopping(tmp_path):
    from backend.pipelines.document import run_pipeline
    pdf = tmp_path / 'blank.pdf'
    pdf.write_bytes(b'PDF content supplied by mocked parser')
    settings = Settings()
    client = SimpleNamespace(health_check=lambda: {'models': list(settings.task_models.values())},
                             request_metrics={'requests': 0, 'elapsed_seconds': 0})
    with patch('backend.stages.pdf_parsing.packets.PdfReader', return_value=reader_for(['', ''])), \
            patch('backend.pipelines.document.PdfReader', return_value=reader_for(['', ''])), \
            patch('backend.pipelines.document.ONTOLOGY_DIR', tmp_path / 'outputs'), \
            patch('backend.pipelines.document.OpenCodeClient', return_value=client), \
            patch('backend.pipelines.document.source_article_identifiers', return_value={}), \
            patch('backend.pipelines.document.extract_claims') as extraction:
        with pytest.raises(ValueError, match='OCR'):
            run_pipeline(pdf, 0, 0, 8, lambda *args: None)
    extraction.assert_not_called()
    output = tmp_path / 'outputs' / 'blank'
    quality = json.loads((output / 'pdf_quality.json').read_text())
    assert quality['pages_with_text'] == 0
    assert len(quality['pages']) == 2
    assert json.loads((output / 'summary.json').read_text())['run_status'] == 'blocked_text_quality'


def test_sentence_pool_round_trip_preserves_negation_conditions_and_reference_links():
    claim, documents, _, _ = fixture()
    document = documents[0]
    common = ('Lead did not reduce microbial activity at the tested low dose in the controlled agricultural soil '
              'experiment after seven days of incubation under the specified temperature and moisture conditions.')
    document['evidence_chunks'] = [
        {'chunk_id': 'p:1', 'text': common + ' Higher doses were examined separately.',
         'paragraph_references': [{'reference_id': 'ref1', 'doi': '10.1234/citation'}]},
        {'chunk_id': 'p:2', 'text': common + ' The microbial assay was performed after seven days.'}]
    original = deepcopy(document)
    numbered = _numbered_chunks(document, claim)
    packed = packed_document(document, claim)
    for chunk, expected in zip(packed['chunks'], numbered):
        restored = {**chunk, 'sentences': [{'number': row['number'], 'text': packed['sentence_pool'][row['sentence_id']]} for row in chunk['sentences']]}
        assert restored == expected
    assert len(packed['sentence_pool']) == 3
    assert document == original
    assert 'sentence_pool' not in packed_document(documents[1], claim)


@pytest.mark.parametrize('problem', ['missing', 'invented_trigger', 'wrong_paper', 'wrong_entity', 'identity_only'])
def test_unquoted_or_misattributed_roles_remain_unresolved(problem):
    claim, documents, response, client = fixture()
    role = response['semantic_assessment']['role_evidence'][0]
    if problem == 'missing':
        response['semantic_assessment']['role_evidence'] = []
    elif problem == 'invented_trigger':
        role['role_trigger'] = 'an essential dietary nutrient'
    elif problem == 'wrong_paper':
        role['evidence_document_id'] = 'paper-b'
    elif problem == 'identity_only':
        role['role_trigger'] = role['entity_mention']
    else:
        role['entity_mention'] = 'Zinc'
    decisions, assessment = judge_evidence_pack(client, claim, documents, Settings())
    assert decisions[0]['decision'] == 'needs_review'
    assert assessment['semantic_assessment']['classification_status'] == 'unresolved'
    assert assessment['semantic_assessment']['role_grounding_errors']
    client.chat_json.assert_called_once()


def test_citation_resolution_rejects_a_different_doi_and_records_unavailability():
    reference = {'reference_number': 1, 'doi': '10.1234/correct', 'citation_text': 'Correct paper'}
    candidate = {'source_references': [reference]}
    ledger = citation_tracking(candidate)[0]
    notes = []
    with patch('backend.stages.literature_retrieval.workflow.resolve_bibliographic_reference', return_value={'doi': reference['doi']}), \
            patch('backend.stages.literature_retrieval.workflow.search', return_value=[{'doi': '10.1234/wrong'}]), \
            patch('backend.stages.literature_retrieval.workflow.openalex_open_access_copy', return_value=None):
        assert not _citation_results(candidate, Settings(), notes, ledger=ledger)
    assert ledger['resolved'] and ledger['status'] == 'unavailable'
    assert not ledger['fetched'] and any(n['reason'] == 'citation_lookup_doi_mismatch' for n in notes)


def test_duplicate_publication_keeps_every_citation_and_missing_link_in_ledger():
    refs = [{'reference_number': n, 'doi': '10.1234/shared', 'citation_text': f'Reference alias {n}'} for n in (1, 2)]
    candidate = {'candidate_id': 'c', 'subject': 'lead', 'object': 'microbial activity', 'relation': 'reduces',
                 'source_references': refs, 'source_citation_links': [{'citation_key': 'baloch|2024', 'status': 'missing_bibliography_link'}]}
    ranker = SimpleNamespace(settings=Settings(), rank_texts=lambda q, texts: [1.0] * len(texts), cross_scores=lambda q, texts: [1.0] * len(texts),
                             select=lambda q, chunks, key, n, **kw: [{'chunk_id': key + ':0', 'text': 'Lead reduced microbial activity.'}])
    def search(query, *args):
        return [{'source': 'MED', 'id': 'shared', 'doi': refs[0]['doi'], 'title': 'Shared publication',
                 'abstractText': 'Lead reduced microbial activity under controlled exposure conditions. Findings were repeated in soil samples.'}] if query.startswith('DOI:') else []
    def judgement(client, claim, documents, settings, nli):
        assert len(documents) == 1
        return ([{'evidence_document_id': documents[0]['evidence_document_id'], 'decision': 'insufficient', 'evidence': []}], {})
    with patch('backend.stages.literature_retrieval.workflow.resolve_bibliographic_reference', return_value={'doi': refs[0]['doi']}), \
            patch('backend.stages.literature_retrieval.workflow.search', side_effect=search), \
            patch('backend.stages.literature_retrieval.workflow.search_openalex', return_value=[]), \
            patch('backend.stages.literature_retrieval.workflow.openalex_open_access_copy', return_value=None), \
            patch('backend.stages.external_validation.workflow.judge_evidence_pack', side_effect=judgement):
        result = validate_candidate(candidate, SimpleNamespace(model=Settings().external_model), ranker,
                                    replace(Settings(), enable_nli=False, external_judgement_mode='joint'), query_translation={'query': 'lead microbial'})
    ledger = result['attempts'][0]['citation_ledger']
    assert len(ledger) == 3
    assert all(row['included_in_judgement'] and row['fetched'] for row in ledger[:2])
    assert ledger[0]['evidence_document_ids'] == ledger[1]['evidence_document_ids']
    assert ledger[2]['status'] == 'missing_bibliography_link'
    assert result['attempts'][0]['paper_contributions'][0]['admissible_quotes'] == 0


def test_timeout_progress_exposes_sizes_and_duration_without_fallback():
    events = []
    settings = replace(Settings(), external_timeout_seconds=1200)
    client = OpenCodeClient(settings, settings.external_model, task='external', on_progress=events.append)
    client.set_request_context({'paper_count': 9, 'chunk_count': 24})
    with patch.object(client._inference, 'chat', side_effect=InferenceError('Request timed out after 1200s', 'timeout')) as call:
        with pytest.raises(LLMServiceError, match='1200s'):
            client.chat_json('instructions', 'evidence', request_label='joint external: c')
    call.assert_called_once()
    assert client.settings.request_timeout_seconds == 1200
    assert [e['status'] for e in events] == ['waiting_for_provider', 'timeout']
    assert events[0]['paper_count'] == 9 and events[0]['chunk_count'] == 24
    assert events[0]['input_tokens'] is None
    assert events[0]['token_count_source'] == 'pending'
    assert events[1]['input_tokens'] is None
    assert events[1]['token_count_source'] == 'unavailable'
    assert 'prompt_characters' not in events[0] and 'input_characters' not in events[0]
    assert events[1]['elapsed_seconds'] >= 0


def test_request_progress_reaches_job_api_and_persistent_state_without_log_duplicates(tmp_path):
    import importlib
    from fastapi.testclient import TestClient
    app = importlib.import_module('backend.api.app')
    event = {'task': 'joint external: c', 'status': 'waiting_for_provider', 'input_tokens': None,
             'input_tokens_reported': False, 'token_count_source': 'pending',
             'paper_count': 9, 'chunk_count': 24, 'timeout_seconds': 1200}
    with patch.dict(app._jobs, {'v34-progress': {'pdf_path': 'test.pdf', 'logs': [], 'status': 'running'}}), \
            patch.object(app, '_output_dir', return_value=tmp_path):
        app._emit('v34-progress', 'progress', json.dumps(event))
        response = TestClient(app.app).get('/api/jobs/v34-progress')
        assert response.status_code == 200
        assert response.json()['request_progress'] == {**event, 'active_request_count': 1}
        assert response.json()['logs'] == []
    from backend.shared.jobs import read_job
    saved = read_job(tmp_path, 'v34-progress')
    assert saved['request_progress']['paper_count'] == 9
    assert saved['request_progress']['status'] == 'waiting_for_provider'
    assert not (tmp_path / 'run_log.jsonl').exists()


def test_neutral_retrieval_does_not_require_agreement_with_claim_direction():
    plan = evidence_query_plan({'subject': 'lead', 'object': 'microbial activity', 'relation': 'reduces'},
                               {'query': 'lead inhibits microbial activity', 'openalex_query': 'lead inhibition microbial activity'})
    assert '"lead" AND "microbial activity"' in plan['europe_pmc']
    assert 'lead microbial activity' in plan['openalex']
