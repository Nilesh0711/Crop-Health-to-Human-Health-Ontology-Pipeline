"""Evidence balances, grouped calls, strict attribution, resumability and provisional isolation."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from backend.config.settings import Settings
from backend.providers.response_repair import validate_schema
from backend.providers.llm import LLMServiceError
from backend.providers.structured_output import EvidenceSelectionError, IncompleteResponseError
from backend.shared.artifacts import save_rows
from backend.stages.trust_graph.ledger import evidence_balance, graph_contributions
from backend.stages.trust_graph.grouping import claim_batches
from backend.stages.trust_graph.judgement import shared_documents
from backend.stages.trust_graph.workflow import assess_group
from backend.stages.pathway_assembly.corpus import collect_corpus
from backend.stages.literature_retrieval.clients import extract_paragraphs, make_chunks
from tests.external.test_paper_synthesis import example, synthesis


def config():
    return replace(Settings(), external_judgement_mode='trust_graph', trust_batch_size=6, enable_nli=False)


def fixtures(count=3, papers=2):
    claims, packets, paper_answers = [], {}, {}
    for n in range(count):
        claim, _, _ = example()
        claim.update(candidate_id=f'claim-{n}', schema_error=None, source_paper_title='Source',
                     source_document_id='source', source_pdf_sha256='source-hash', source_page=1)
        claims.append(claim)
        docs = []
        for p in range(papers):
            _, doc, answer = example(f'paper-{p}')
            docs.append(doc); paper_answers[(claim['candidate_id'], doc['evidence_document_id'])] = answer
        packets[claim['candidate_id']] = {'evidence_documents': docs, 'citation_ledger': [], 'search_paper_budget': 8}
    return claims, packets, paper_answers


class GroupClient:
    def __init__(self, answers, model=None, state=None):
        self.settings = config()
        self.model = model or self.settings.external_model
        self.answers = answers
        self.state = state if state is not None else {'calls': [], 'fail': None, 'corrupt': None}
        self.last_request_metrics = {'model': self.model}

    def for_model(self, model):
        return GroupClient(self.answers, model, self.state)

    def set_request_context(self, context):
        self.context = context

    def chat_json(self, system, user, **options):
        data = json.loads(user)
        self.state['calls'].append((self.model, data, options['request_label']))
        if self.state['fail'] and self.state['fail'] in options['request_label']:
            raise LLMServiceError('Synthetic interruption')
        if options['request_label'].startswith('grouped paper'):
            paper = data['documents'][0]['evidence_document_id']
            rows = [{**deepcopy(self.answers[(c['candidate_id'], paper)]), 'candidate_id': c['candidate_id']}
                    for c in data.get('claims', [data.get('claim')])]
            if self.state['corrupt'] == 'paper':
                rows[0]['decision']['evidence'][0]['object_mention'] = 'fabricated text'
            if self.state['corrupt'] == 'duplicate':
                rows[-1]['candidate_id'] = rows[0]['candidate_id']
            if 'claim' in data:
                answer = {k: v for k, v in rows[0].items() if k != 'candidate_id'}
            else:
                answer = {'results': {r['candidate_id']: {k: v for k, v in r.items() if k != 'candidate_id'} for r in rows}}
        else:
            rows = [{'candidate_id': c['candidate_id'],
                     **synthesis([d for d in data['documents'] if d['evidence_document_id'] in c['evidence_document_ids']])}
                    for c in data['claims']]
            answer = {'results': rows, 'pathway_connections': [
                {'upstream_claim_id': r['upstream_claim_id'], 'downstream_claim_id': r['downstream_claim_id'],
                 'compatibility': 'unresolved', 'reason': 'Whole-chain compatibility not established.',
                 'evidence_document_ids': []} for r in data['pathway_connections']]}
        validate_schema(answer, options['json_schema'])
        options['response_check'](answer)
        return answer


def test_five_claims_share_two_paper_calls_and_one_synthesis_without_losing_verdicts(tmp_path):
    claims, packets, answers = fixtures(5)
    client, saved = GroupClient(answers), []
    results, _, _ = assess_group(claims, packets, client, config(), tmp_path, on_checkpoint=saved.append)
    assert len(client.state['calls']) == 3  # previous architecture: 5 * (2 + 1)
    assert [m for m, _, _ in client.state['calls']] == [config().external_paper_model] * 2 + [config().external_model]
    assert len(results) == 5 and all(r['trust']['score'] == 2 for r in results)
    assert all(r['trust']['assessment_complete'] for r in results)
    assert all(len(r['attempts'][0]['decisions']) == 2 for r in results)
    assert all(r['validation_status'] == 'world_independently_corroborated_pending_review' for r in results)
    calls = len(client.state['calls'])
    again, _, stats = assess_group(claims, deepcopy(packets), client, config(), tmp_path)
    assert len(client.state['calls']) == calls and stats['reused_paper_assessments'] == 10
    assert [r['trust'] for r in again] == [r['trust'] for r in results]


def test_real_provider_adapter_preserves_primary_routes_thinking_and_response_modes(tmp_path, monkeypatch):
    from backend.providers.inference import InferenceHTTP
    from backend.providers.llm import OpenCodeClient
    claims, packets, answers = fixtures(2, 1)
    paper = {'results': {c['candidate_id']: answers[(c['candidate_id'], 'paper-0')] for c in claims}}
    final = {'results': [{'candidate_id': c['candidate_id'], **synthesis(packets[c['candidate_id']]['evidence_documents'])} for c in claims],
             'pathway_connections': []}
    replies, payloads, metrics = iter([paper, final]), [], []
    def receive(self, method, url, **kwargs):
        payloads.append(kwargs['json'])
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(next(replies))}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 30, 'completion_tokens_details': {'reasoning_tokens': 0}}}
    monkeypatch.setattr(InferenceHTTP, '_request', receive)
    client = OpenCodeClient(config(), task='external', on_request=metrics.append)
    results, _, stats = assess_group(claims, packets, client, config(), tmp_path)
    assert [p['model'] for p in payloads] == ['deepseek-v4.1-flash', 'mimo-v2.6-pro']
    assert [p['response_format']['type'] for p in payloads] == ['json_object', 'json_schema']
    assert all(p['thinking'] == {'type': 'disabled'} for p in payloads)
    assert all(m['primary_model'] == m['model'] and m['next_model'] is None for m in metrics)
    assert stats['primary_calls'] == 2 and stats['comparison_unbatched_calls'] == 4
    assert all(r['trust']['score'] == 1 for r in results)


def test_shared_paper_wire_contract_has_one_claim_identity_and_rejects_wrong_envelopes(tmp_path):
    from backend.stages.trust_graph.judgement import judge_shared_paper
    from backend.providers.structured_output import parse_json_object, DuplicateJSONPropertyError
    claims, packets, answers = fixtures(2, 1)
    captured = {}
    class Inspect:
        def chat_json(self, system, user, **options):
            schema = options['json_schema']
            assert 'Judge ONE fixed scientific claim' not in system
            assert schema['properties']['results']['type'] == 'object'
            assert 'candidate_id' not in schema['$defs']['paper_assessment']['properties']
            value = {'results': {c['candidate_id']: answers[(c['candidate_id'], 'paper-0')] for c in claims}}
            validate_schema(value, schema)
            captured.update(schema=schema, value=deepcopy(value))
            options['response_check'](value)
            return value
    docs = {cid: packet['evidence_documents'][0] for cid, packet in packets.items()}
    result = judge_shared_paper(Inspect(), claims, docs['claim-0'], docs, config())
    assert [r['candidate_id'] for r in result['results']] == ['claim-0', 'claim-1']
    for row in result['results']:
        assert {k: v for k, v in row.items() if k != 'candidate_id'} == answers[(row['candidate_id'], 'paper-0')]
    bad = deepcopy(captured['value']); bad['results']['unknown'] = bad['results'].pop('claim-1')
    with pytest.raises(ValueError):
        validate_schema(bad, captured['schema'])
    bad = deepcopy(captured['value']); bad['results']['claim-0']['candidate_id'] = 'claim-0'
    with pytest.raises(ValueError):
        validate_schema(bad, captured['schema'])
    # Even identical duplicate IDs stay ambiguous JSON, never auto-repaired.
    row = json.dumps(captured['value']['results']['claim-0'])
    with pytest.raises(DuplicateJSONPropertyError):
        parse_json_object('{"results":{"claim-0":' + row + ',"claim-0":' + row + '}}')


def test_single_claim_has_no_identity_envelope_and_keeps_large_generation_allowance(monkeypatch):
    from backend.providers.inference import InferenceHTTP
    from backend.providers.llm import OpenCodeClient
    from backend.stages.trust_graph.judgement import judge_shared_paper
    claims, packets, answers = fixtures(1, 1)
    expected = answers[('claim-0', 'paper-0')]
    payloads = []
    def receive(self, method, url, **kwargs):
        payloads.append(kwargs['json'])
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(expected)}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 50, 'completion_tokens_details': {'reasoning_tokens': 0}}}
    monkeypatch.setattr(InferenceHTTP, '_request', receive)
    client = OpenCodeClient(config(), task='external', model=config().external_paper_model)
    doc = packets['claim-0']['evidence_documents'][0]
    result = judge_shared_paper(client, claims, doc, {'claim-0': doc}, config())
    assert result == {'results': [{'candidate_id': 'claim-0', **expected}]}
    assert len(payloads) == 1
    wire = payloads[0]
    assert wire['max_tokens'] == config().external_min_output_budget + 4096
    instructions = wire['messages'][0]['content']
    assert '2600 answer tokens' in instructions and 'not a target length' in instructions
    assert '28192 answer tokens' not in instructions
    schema = json.loads(instructions.split('Response JSON Schema:\n')[1].split('\nKeep the final answer')[0])
    assert set(schema['properties']) == {'decision', 'finding'}
    assert json.loads(wire['messages'][1]['content'])['claim']['candidate_id'] == 'claim-0'
    assert client.last_request_metrics['status'] == 'ok'


def test_duplicate_single_claim_envelope_regenerates_to_bare_result_without_local_repair(monkeypatch):
    from backend.providers.inference import InferenceHTTP
    from backend.providers.llm import OpenCodeClient
    from backend.stages.trust_graph.judgement import judge_shared_paper
    claims, packets, answers = fixtures(1, 1)
    expected = answers[('claim-0', 'paper-0')]
    encoded = json.dumps(expected)
    malformed = '{"results":{"claim-0":' + encoded + ',"claim-0":' + encoded + '}}'
    replies, requests, metrics = iter([malformed, encoded]), [], []
    def receive(self, method, url, **kwargs):
        requests.append(kwargs['json'])
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': next(replies)}}]}
    monkeypatch.setattr(InferenceHTTP, '_request', receive)
    client = OpenCodeClient(config(), task='external', model=config().external_paper_model, on_request=metrics.append)
    doc = packets['claim-0']['evidence_documents'][0]
    result = judge_shared_paper(client, claims, doc, {'claim-0': doc}, config())
    assert result == {'results': [{'candidate_id': 'claim-0', **expected}]}
    assert len(requests) == 2
    assert requests[0]['messages'][1] == requests[1]['messages'][1]  # Original evidence, no prior answer pasted.
    assert [m['purpose'] for m in metrics] == ['judgement', 'response_regeneration']
    assert metrics[0]['error'] == 'Duplicate JSON property: claim-0'


def test_shared_paper_rejects_duplicate_input_claims_before_provider():
    from backend.stages.trust_graph.judgement import judge_shared_paper
    claims, packets, answers = fixtures(1, 1)
    client = GroupClient(answers)
    doc = packets['claim-0']['evidence_documents'][0]
    with pytest.raises(ValueError, match='distinct claim identities'):
        judge_shared_paper(client, [claims[0], claims[0]], doc, {'claim-0': doc}, config())
    assert client.state['calls'] == []


def test_paper_requests_overlap_but_results_keep_the_original_paper_order(tmp_path):
    from threading import Barrier, Lock
    claims, packets, answers = fixtures(2, 3)
    client = GroupClient(answers)
    barrier, lock = Barrier(3, timeout=3), Lock()
    counts = {'active': 0, 'peak': 0}
    original = client.chat_json
    def receive(system, user, **options):
        if options['request_label'].startswith('grouped paper'):
            with lock:
                counts['active'] += 1
                counts['peak'] = max(counts['peak'], counts['active'])
            barrier.wait()
            try:
                return original(system, user, **options)
            finally:
                with lock:
                    counts['active'] -= 1
        assert counts['active'] == 0  # Pro waits for all selected papers.
        return original(system, user, **options)
    client.for_model = lambda model: type('PaperClient', (), {
        'model': model, 'chat_json': staticmethod(receive), 'last_request_metrics': {'model': model}})()
    results, _, stats = assess_group(claims, packets, client, config(), tmp_path)
    assert counts['peak'] == stats['paper_workers'] == 3
    assert all([d['evidence_document_id'] for d in r['attempts'][0]['decisions']] == ['paper-0', 'paper-1', 'paper-2'] for r in results)
    assert stats['primary_calls'] == 4 and all(r['trust']['score'] == 3 for r in results)


def test_grouped_synthesis_shares_identical_source_context_without_dropping_text(tmp_path):
    claims, packets, answers = fixtures(3, 1)
    context = 'Exact shared source context. Conditions and limitations remain present.'
    for c in claims:
        c['source_section_context'] = context
    client = GroupClient(answers)
    assess_group(claims, packets, client, config(), tmp_path)
    payload = next(data for _, data, label in client.state['calls'] if label.startswith('grouped cross'))
    assert list(payload['source_context_pool'].values()) == [context]
    for row in payload['claims']:
        assert payload['source_context_pool'][row['source_scope_context_only']['local_context_ref']] == context


def test_parallel_scheduler_preserves_job_cancellation_and_stops_submitting_after_failure():
    from backend.stages.trust_graph.scheduling import parallel_papers
    from backend.shared.cancellation import CancellationToken, cancellation_scope, PipelineStopped, current_token
    token = CancellationToken()
    with cancellation_scope(token):
        assert dict(parallel_papers([1, 2], lambda _: current_token() is token, 2)) == {1: True, 2: True}
        token.request()
        with pytest.raises(PipelineStopped):
            list(parallel_papers([1], lambda _: None, 2))
    calls = []
    def fail(item):
        calls.append(item)
        raise LLMServiceError('Test failure')
    with pytest.raises(LLMServiceError):
        list(parallel_papers(range(10), fail, 2))
    assert set(calls) <= {0, 1}  # No hidden retries or unbounded queued calls.


def test_interrupted_synthesis_reuses_all_completed_papers(tmp_path):
    claims, packets, answers = fixtures(3)
    client = GroupClient(answers); client.state['fail'] = 'grouped cross'
    saved = []
    with pytest.raises(LLMServiceError):
        assess_group(claims, packets, client, config(), tmp_path, on_checkpoint=saved.append)
    assert all(r['validation_status'] == 'pipeline_error' for r in saved[-3:])
    assert all(not r['trust']['assessment_complete'] for r in saved[-3:])
    client.state['fail'] = None
    results, _, stats = assess_group(claims, packets, client, config(), tmp_path)
    assert stats['reused_paper_assessments'] == 6
    assert len(client.state['calls']) == 4  # two papers, failed Pro, resumed Pro
    assert all(r['trust']['score'] == 2 for r in results)


def test_partial_paper_failure_does_not_count_as_contradiction_or_discard_completed_papers(tmp_path):
    claims, packets, answers = fixtures(2)
    client = GroupClient(answers); client.state['fail'] = 'paper-1'
    saved = []
    with pytest.raises(LLMServiceError):
        assess_group(claims, packets, client, config(), tmp_path, on_checkpoint=saved.append)
    assert all(r['trust']['score'] == 1 and r['trust']['contradiction_count'] == 0 for r in saved[-2:])
    client.state['fail'] = None
    results, _, stats = assess_group(claims, packets, client, config(), tmp_path)
    assert stats['reused_paper_assessments'] == 2
    assert all(r['trust']['score'] == 2 for r in results)


def test_oversized_input_fails_before_provider_call_without_dropping_evidence(tmp_path):
    claims, packets, answers = fixtures(2)
    client = GroupClient(answers)
    settings = replace(config(), external_input_max_characters=1000)
    with pytest.raises(LLMServiceError, match='no claim or paper was dropped'):
        assess_group(claims, packets, client, settings, tmp_path)
    assert not client.state['calls']


def test_pathway_compatibility_is_preserved_and_invalidated_when_an_edge_is_revised(tmp_path):
    from backend.stages.trust_graph.ledger import connection_assessments
    claims, packets, answers = fixtures(2)
    client = GroupClient(answers)
    link = {'upstream_claim_id': 'claim-0', 'downstream_claim_id': 'claim-1', 'shared_entity': 'Shared context'}
    results, links, _ = assess_group(claims, packets, client, config(), tmp_path / 'cache', links=[link])
    save_rows(tmp_path / 'doc' / 'candidates.jsonl', claims)
    save_rows(tmp_path / 'doc' / 'validation_runs.jsonl', results)
    save_rows(tmp_path / 'doc' / 'trust_connections.jsonl', links)
    assert connection_assessments(tmp_path)[0]['compatibility'] == 'unresolved'
    assert not connection_assessments(tmp_path)[0]['causal_chain_validated']
    results[0]['validation_status'] = 'pipeline_error'
    save_rows(tmp_path / 'doc' / 'validation_runs.jsonl', results)
    assert connection_assessments(tmp_path) == []


@pytest.mark.parametrize('corrupt,exception', [('paper', EvidenceSelectionError), ('duplicate', ValueError)])
def test_grouped_proof_does_not_accept_invented_mentions_or_duplicate_claim_binding(tmp_path, corrupt, exception):
    claims, packets, answers = fixtures(2)
    client = GroupClient(answers); client.state['corrupt'] = corrupt
    with pytest.raises(exception):
        assess_group(claims, packets, client, config(), tmp_path)
    assert not list(tmp_path.glob('*.json'))


def test_empty_discovery_keeps_claims_unassessed_and_spends_no_judgement_calls(tmp_path):
    claims, packets, answers = fixtures(2, 0)
    client = GroupClient(answers)
    results, links, _ = assess_group(claims, packets, client, config(), tmp_path)
    assert not client.state['calls'] and not links
    assert all(r['trust']['score'] == 0 and r['trust']['status'] == 'not_assessed' for r in results)
    assert all(r['validation_status'] == 'retrieval_failed_or_no_open_access_evidence' for r in results)


def test_same_study_multiple_papers_or_packets_adds_only_one_vote_and_reviews_add_none():
    claims, packets, answers = fixtures(1, 3)
    c = claims[0]; docs = packets[c['candidate_id']]['evidence_documents']
    for doc in docs[:2]: doc['independence_group'] = 'one-underlying-study'
    decisions = []
    for doc in docs:
        a = answers[(c['candidate_id'], doc['evidence_document_id'])]
        decisions.append({'evidence_document_id': doc['evidence_document_id'], **a['decision'], 'finding': a['finding']})
    decisions[-1]['finding']['evidence_origin'] = 'review_summary'
    attempt = {'evidence_documents': docs, 'decisions': decisions}
    result = {'validation_status': 'complete', 'attempts': [attempt, deepcopy(attempt)]}
    trust = evidence_balance(c, result)
    assert trust['score'] == 1 and trust['support_count'] == 1 and len(trust['findings']) == 3


def test_conflicting_evidence_is_visible_even_with_positive_balance():
    claims, packets, answers = fixtures(1, 3)
    c = claims[0]; docs = packets[c['candidate_id']]['evidence_documents']
    decisions = []
    for i, doc in enumerate(docs):
        a = deepcopy(answers[(c['candidate_id'], doc['evidence_document_id'])])
        if i == 2:
            a['decision'].update(decision='contradicted')
            a['decision']['matches']['relation'] = False
            a['decision']['evidence'][0]['stance'] = 'contradicts'
        decisions.append({'evidence_document_id': doc['evidence_document_id'], **a['decision'], 'finding': a['finding']})
    trust = evidence_balance(c, {'validation_status': 'contested', 'attempts': [{'evidence_documents': docs, 'decisions': decisions}]})
    assert trust['score'] == 1 and trust['status'] == 'contested'
    assert trust['support_count'] == 2 and trust['contradiction_count'] == 1


def test_graph_centrality_never_changes_evidence_balance_and_disconnected_claims_remain():
    claims, _, _ = fixtures(3)
    graph = {'edges': [{'id': 'e1', 'candidate_id': 'claim-0', 'document_key': 'doc', 'source': 'a', 'target': 'b'},
                       {'id': 'e2', 'candidate_id': 'claim-1', 'document_key': 'doc', 'source': 'b', 'target': 'c'}]}
    contributions = graph_contributions(graph)
    assert contributions['e1']['two_edge_connections'] == 1
    assert evidence_balance(claims[0], contribution=contributions['e1'])['score'] == 0
    batches = list(claim_batches(claims, graph, 'doc', 2))
    assert [[c['candidate_id'] for c in b] for b in batches] == [['claim-0', 'claim-1'], ['claim-2']]


def test_conflicting_shared_chunk_identity_is_rejected():
    _, doc, _ = example()
    other = deepcopy(doc); other['evidence_chunks'][0]['text'] = 'A different finding.'
    with pytest.raises(ValueError, match='different source text'):
        shared_documents([doc, other])


def test_changed_assertion_cannot_keep_visible_votes_or_validated_graph_membership(tmp_path):
    from backend.stages.graph_construction.graph import graph_from_candidates
    claims, packets, answers = fixtures(1, 1)
    results, _, _ = assess_group(claims, packets, GroupClient(answers), config(), tmp_path)
    assert evidence_balance(claims[0], results[0])['score'] == 1
    claims[0]['qualifiers']['condition'] = 'A different biological restriction'
    balance = evidence_balance(claims[0], results[0])
    assert balance['score'] == 0 and not balance['assessment_complete']
    assert balance['assessment_status'] == 'stale_claim_requires_reassessment'
    assert graph_from_candidates(claims, results)['edges'] == []


def test_provisional_corpus_includes_pending_edges_but_validated_corpus_does_not(tmp_path):
    claims, _, _ = fixtures(2, 0)
    save_rows(tmp_path / 'doc' / 'candidates.jsonl', claims)
    assert len(collect_corpus(tmp_path, provisional=True)[0]) == 2
    assert collect_corpus(tmp_path)[0] == []


def test_xml_tables_preserve_headers_cells_units_footnotes_and_figure_captions():
    xml = '<article><body><table-wrap id="T1"><caption><p>Yield outcomes</p></caption><table><thead><tr><th>Site</th><th>Yield loss (%)</th></tr></thead><tbody><tr><td>Site A</td><td>1.0</td></tr><tr><td>Site B</td><td>13.5</td></tr></tbody></table><table-wrap-foot><p>Observed field losses.</p></table-wrap-foot></table-wrap><fig id="F1"><caption><p>Measured outcome under the stated treatment.</p></caption></fig></body></article>'
    blocks = extract_paragraphs(xml, with_sections=True)
    tables = [b for b in blocks if b['source_kind'] == 'table_row']
    assert len(tables) == 2 and tables[1]['table_cells'][1]['text'] == '13.5'
    assert tables[0]['table_headers'][0][1]['text'] == 'Yield loss (%)'
    assert tables[1]['table_footnotes'] == ['Observed field losses.']
    assert any(b['source_kind'] == 'figure_caption' for b in blocks)
    assert make_chunks(tables)[1]['table_id'] == 'T1'
