"""Joint judgement must keep evidence, identities and correction gates grounded."""

from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from backend.config.settings import Settings
from backend.stages.external_validation.joint import (
    judge_evidence_pack,
    verified_contradictions,
    explicit_conflict,
)
from backend.providers.llm import LLMServiceError
from backend.stages.literature_retrieval.clients import (
    extract_paragraphs,
    resolve_bibliographic_reference,
    search_openalex,
)
from backend.stages.literature_retrieval.workflow import retrieve_round
from backend.stages.evidence_ranking.articles import _bm25_scores


def fixture():
    claim = {'candidate_id': 'c', 'subject': 'lead', 'object': 'microbial activity',
             'relation': 'reduces', 'subject_type': 'Contaminant', 'object_type': 'SoilBiota',
             'certainty': 'descriptive', 'qualifiers': {}}
    documents, rows = [], []
    for identifier, verb, verdict in [('paper-a', 'reduced', 'supported'), ('paper-b', 'increased', 'contradicted')]:
        quote = f'Lead {verb} microbial activity.'
        documents.append({'evidence_document_id': identifier, 'evidence_level': 'independent_external',
                          'independence_group': identifier,
                          'evidence_chunks': [{'chunk_id': identifier + ':0', 'text': quote}]})
        rows.append({'evidence_document_id': identifier, 'decision': verdict,
            'matches': {k: (k != 'direction' or verdict == 'supported') for k in ('subject', 'object', 'relation', 'direction', 'certainty')},
            'entity_alignment': {role: {'canonical': canonical, 'mention': mention, 'kind': 'same_concept',
                                       'evidence': quote, 'explanation': 'Exact phrase in the selected quote'}
                                 for role, canonical, mention in [('subject', 'lead', 'Lead'), ('object', 'microbial activity', 'microbial activity')]},
            'qualifier_alignment': {}, 'rationale': 'Explicit direction in the selected result.',
            'evidence': [{'chunk_id': identifier + ':0', 'sentence_numbers': [1], 'subject_mention': 'Lead',
                          'object_mention': 'microbial activity', 'relation_trigger': verb,
                          'stance': 'supports' if verdict == 'supported' else 'contradicts'}]})
    response = {'documents': rows, 'semantic_assessment': {'subject_type': 'Contaminant', 'object_type': 'SoilBiota',
                 'in_scope': True, 'ambiguous': False, 'evidence_document_ids': ['paper-a', 'paper-b'],
                 'rationale': 'Compatible measured concepts.', 'role_evidence': [
                     {'role': role, 'evidence_document_id': 'paper-a', 'chunk_id': 'paper-a:0', 'sentence_numbers': [1],
                      'entity_mention': mention, 'role_trigger': 'reduced microbial activity', 'rationale': 'Measured adverse effect context.'}
                     for role, mention in [('subject', 'Lead'), ('object', 'microbial activity')]]}, 'synthesis': 'The supplied experiments disagree.'}
    client = SimpleNamespace(model=Settings().external_model, settings=Settings(),
                             last_request_metrics={'model': Settings().external_model, 'status': 'ok'},
                             chat_json=Mock(return_value=response))
    return claim, documents, response, client


def test_one_provider_call_sees_all_papers_and_keeps_both_stances():
    claim, documents, _, client = fixture()
    decisions, assessment = judge_evidence_pack(client, claim, documents, Settings())
    client.chat_json.assert_called_once()
    payload = json.loads(client.chat_json.call_args.args[1])
    assert len(payload['documents']) == 2
    assert [d['decision'] for d in decisions] == ['supported', 'contradicted']
    assert decisions[0]['evidence'][0]['quoted_span'] == 'Lead reduced microbial activity.'
    assert all(d['judge']['tier'] == 'joint' for d in decisions)
    assert not assessment['semantic_review_required']


def test_removing_semantic_gate_preserves_the_source_supported_graph():
    from backend.stages.graph_construction.graph import graph_from_candidates
    claim, _, _, _ = fixture()
    claim.update(semantic_validation={'decision': 'not_run'},
                 source_validation={'decision': 'entailed', 'rationale': 'The PDF states this claim.'})
    graph = graph_from_candidates([claim], [], view='source')
    assert graph['counts']['source_supported'] == 1
    claim['schema_error'] = 'Illegal type combination'
    assert graph_from_candidates([claim], [], view='source')['counts']['source_supported'] == 0


def test_quote_from_another_paper_cannot_fill_missing_proof():
    claim, documents, response, client = fixture()
    response['documents'][0]['evidence'][0]['chunk_id'] = 'paper-b:0'
    decisions, _ = judge_evidence_pack(client, claim, documents, Settings())
    assert decisions[0]['decision'] == 'needs_review'
    assert decisions[0]['evidence'] == []
    assert decisions[1]['decision'] == 'contradicted'


@pytest.mark.parametrize('mutate', ['missing', 'duplicate', 'unknown'])
def test_incomplete_or_invented_document_ledger_is_not_accepted(mutate):
    claim, documents, response, client = fixture()
    if mutate == 'missing':
        response['documents'].pop()
    else:
        response['documents'][1]['evidence_document_id'] = 'paper-a' if mutate == 'duplicate' else 'invented'
    with pytest.raises(LLMServiceError, match='exactly once'):
        judge_evidence_pack(client, claim, documents, Settings())


def test_large_pack_is_never_silently_trimmed():
    claim, documents, _, client = fixture()
    documents[0]['evidence_chunks'][0]['text'] *= 1000
    with pytest.raises(LLMServiceError, match='No paper was silently dropped'):
        judge_evidence_pack(client, claim, documents, replace(Settings(), external_input_max_characters=1000))
    client.chat_json.assert_not_called()


def test_types_are_assessed_with_external_evidence_and_ambiguity_requires_review():
    claim, documents, response, client = fixture()
    response['semantic_assessment']['ambiguous'] = True
    decisions, assessment = judge_evidence_pack(client, claim, documents, Settings())
    assert decisions[0]['decision'] == 'needs_review'
    assert assessment['semantic_review_required']


def test_same_direction_quote_is_not_admitted_as_a_contradiction():
    claim, documents, response, client = fixture()
    response['documents'][0]['decision'] = 'contradicted'
    response['documents'][0]['evidence'][0]['stance'] = 'contradicts'
    decisions, _ = judge_evidence_pack(client, claim, documents, Settings())
    assert decisions[0]['decision'] == 'needs_review'
    assert not explicit_conflict(claim, {'evidence': [{'relation_trigger': 'did not increase'}]})
    assert explicit_conflict(claim, decisions[1])


def test_external_repair_needs_real_quoted_contradiction():
    claim, documents, _, client = fixture()
    decisions, _ = judge_evidence_pack(client, claim, documents, Settings())
    validation = {'validation_status': 'world_contested_pending_review',
                  'attempts': [{'evidence_documents': documents, 'decisions': decisions}]}
    assert len(verified_contradictions(claim, validation)) == 1
    corrupted = deepcopy(validation)
    corrupted['attempts'][0]['decisions'][1]['evidence'][0]['quoted_span'] = 'Invented quotation.'
    assert verified_contradictions(claim, corrupted) == []


def test_claim_api_exposes_only_verified_external_repair_proof(tmp_path):
    import importlib
    from fastapi.testclient import TestClient
    from backend.shared.storage import write_jsonl
    app_module = importlib.import_module('backend.api.app')
    claim, documents, _, client = fixture()
    claim['source_validation'] = {'decision': 'entailed'}
    decisions, _ = judge_evidence_pack(client, claim, documents, Settings())
    validation = {'candidate_id': claim['candidate_id'], 'validation_status': 'world_contested_pending_review',
                  'attempts': [{'evidence_documents': documents, 'decisions': decisions}]}
    write_jsonl(tmp_path / 'candidates.jsonl', [claim])
    write_jsonl(tmp_path / 'validation_runs.jsonl', [validation])
    with patch.object(app_module, '_pdf_path', return_value=tmp_path / 'paper.pdf'), \
            patch.object(app_module, '_output_dir', return_value=tmp_path):
        http = TestClient(app_module.app)
        eligibility = http.get('/api/pdfs/paper/claims').json()['claims'][0]['repair_eligibility']
        assert eligibility['allowed']
        assert eligibility['external_contradictions'][0]['evidence'][0]['quoted_span'] == 'Lead increased microbial activity.'
        decisions[1]['evidence'][0]['quoted_span'] = 'Invented quotation.'
        write_jsonl(tmp_path / 'validation_runs.jsonl', [validation])
        eligibility = http.get('/api/pdfs/paper/claims').json()['claims'][0]['repair_eligibility']
        assert not eligibility['allowed']
        assert eligibility['external_contradictions'] == []


def test_embedded_bibliography_doi_bypasses_fuzzy_search():
    with patch('backend.stages.literature_retrieval.clients.fetch_json') as fetch:
        result = resolve_bibliographic_reference({'citation_text': 'Author 2020. Some title. https://doi.org/10.1234/example.'}, Settings())
    fetch.assert_not_called()
    assert result['doi'] == '10.1234/example'
    assert result['resolution_method'] == 'bibliography_doi'


def test_crossref_match_requires_title_author_and_year():
    citation = {'citation_text': 'Smith 2020. Lead effects on microbial activity in soil.'}
    good = {'DOI': '10.1234/example', 'title': ['Lead effects on microbial activity in soil'],
            'author': [{'family': 'Smith'}], 'published-print': {'date-parts': [[2020]]}}
    with patch('backend.stages.literature_retrieval.clients.fetch_json', return_value={'message': {'items': [good]}}):
        assert resolve_bibliographic_reference(citation, Settings())['doi'] == '10.1234/example'
    good['author'] = [{'family': 'Jones'}]
    with patch('backend.stages.literature_retrieval.clients.fetch_json', return_value={'message': {'items': [good]}}):
        assert resolve_bibliographic_reference(citation, Settings()) is None


def test_openalex_does_not_require_an_abstract_to_discover_pdf_only_work():
    with patch('backend.stages.literature_retrieval.clients.fetch_json', return_value={'results': []}) as fetch:
        search_openalex('lead soil microbial activity', Settings(), 100)
    assert 'has_abstract' not in fetch.call_args.args[0]


def test_xml_reference_links_are_navigation_not_proof_paragraphs():
    xml = '''<article><body><sec><title>Results</title><p>Lead treatment reduced microbial activity under the tested exposure conditions; earlier evidence was reported in <xref ref-type="bibr" rid="r1">1</xref>.</p></sec></body><back><ref-list><ref id="r1"><mixed-citation>Smith 2020. Earlier study. <pub-id pub-id-type="doi">10.1234/earlier</pub-id></mixed-citation></ref></ref-list></back></article>'''
    paragraphs = extract_paragraphs(xml, with_sections=True)
    assert len(paragraphs) == 1
    assert paragraphs[0]['paragraph_references'][0]['doi'] == '10.1234/earlier'
    assert 'Earlier study' not in paragraphs[0]['text']


def test_all_attached_citations_are_considered_not_just_one_or_six():
    references = [{'reference_number': i, 'citation_text': f'Study {i}', 'doi': f'10.1234/ref{i}'} for i in range(9)]
    records = [{'id': f'paper{i}', 'source': 'MED', 'doi': ref['doi'], 'title': f'Study {i}',
                'abstractText': 'Lead reduced soil microbial activity under the stated conditions in this controlled experiment.' * 2,
                '_retrieval_route': 'source_citation', '_source_reference_number': i} for i, ref in enumerate(references)]
    ranker = SimpleNamespace(settings=Settings(), rank_texts=lambda q, texts: [1.0] * len(texts),
                             cross_scores=lambda q, texts: [1.0] * len(texts),
                             select=lambda q, chunks, key, n, **kw: [{'chunk_id': key + ':0', 'text': str(chunks[0])}])
    def resolve(candidate, *args):
        number = candidate['source_references'][0]['reference_number']
        return [records[number]]
    with patch('backend.stages.literature_retrieval.workflow._citation_results', side_effect=resolve) as lookup, \
            patch('backend.stages.literature_retrieval.workflow.search', return_value=[]), \
            patch('backend.stages.literature_retrieval.workflow.search_openalex', return_value=[]):
        result = retrieve_round({'candidate_id': 'claim', 'subject': 'lead', 'object': 'soil microbial activity',
                                 'relation': 'reduces', 'source_references': references}, None, ranker, Settings(), 8, 4,
                                query_translation={'query': 'lead soil'})
    assert lookup.call_count == 9
    assert len(result['evidence_documents']) == 9


def test_bm25_exact_tokens_do_not_invent_chemical_synonyms():
    scores = _bm25_scores('lead', ['lead in soil', 'Pb in soil', 'lead lead lead in soil'])
    assert scores[0] > 0 and scores[1] == 0
    assert scores[2] > scores[0]
