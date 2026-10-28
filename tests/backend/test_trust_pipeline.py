"""The new document flow preserves source gates and builds provisional identities first."""
from copy import deepcopy

from backend.shared.storage import read_jsonl
from backend.shared.artifacts import save_rows
from backend.stages.pathway_assembly.corpus import collect_corpus, corpus_signature, ASSEMBLY_VERSION
from backend.stages.pathway_assembly.assembly import read_assembly
from backend.stages.source_validation.reassessment import reassess_source_claim
from tests.backend.test_pipeline_resume import run_fixture
from tests.backend.test_source_abbreviations import fixture, Client as SourceClient


def test_document_trust_mode_builds_provisional_graph_before_shared_assessment_and_resumes(run_fixture, monkeypatch):
    case = run_fixture
    calls, events = [], []
    def assemble(root, client, settings, emit, *, provisional=False):
        assert provisional is True
        calls.append('provisional')
        edges, endpoints = collect_corpus(root, provisional=True)
        labels = sorted({e[role] for e in edges for role in ('subject', 'object')})
        nodes = [{'id': label, 'label': label, 'type': 'Unresolved',
                  'endpoint_ids': [r['endpoint_id'] for r in endpoints if r['name'] == label]} for label in labels]
        return {'version': ASSEMBLY_VERSION, 'model': settings.pathway_assembly_model,
                'thinking': settings.task_thinking['pathway_assembly'], 'endpoints': endpoints,
                'input_signature': corpus_signature(edges, endpoints, settings.pathway_assembly_model, settings.task_thinking['pathway_assembly'], provisional=True),
                'graph': {'nodes': nodes, 'edges': [{**e, 'source': e['subject'], 'target': e['object']} for e in edges]},
                'counts': {'claims': len(edges), 'entities': len(nodes), 'papers': 1}}
    monkeypatch.setattr(case.module, 'assemble_pathways', assemble)
    monkeypatch.setattr(case.module.OpenCodeClient, 'for_model', lambda self, model: self, raising=False)
    monkeypatch.setattr('backend.pipelines.trust.prepare_evidence', lambda *a, **kw: {'evidence_documents': []})
    def run(resume=False):
        return case.module.run_pipeline(case.pdf, 0, 0, 8, lambda level, message: events.append(str(message)),
            claims_per_packet=4, resume=resume, knowledge_building_mode='trust_graph', trust_batch_size=2)
    result = run()
    assert result['knowledge_building_mode'] == 'trust_graph'
    assert result['source_entailed'] == 2
    assert calls == ['provisional']  # validated membership projection has no second model call
    assert all(r['trust']['score'] == 0 for r in read_jsonl(case.output / 'validation_runs.jsonl'))
    assert len(read_jsonl(case.output / 'trust_scores.jsonl')) == 2
    data = read_assembly(case.output.parent, case.settings.pathway_assembly_model)
    assert data['snapshot']['counts']['claims'] == 0  # no papers => never validated
    assert not data['stale']
    again = run(resume=True)
    assert again['extracted'] == 2 and case.calls['extraction'] == 1
    assert len(case.calls['source']) == 2 and len(case.calls['query']) == 2
    assert any('Assembling provisional' in e for e in events)


def test_source_reassessment_retains_the_original_and_can_confirm_rejection():
    claim, response = fixture()
    original = {'candidate_id': claim['candidate_id'], 'decision': 'unsupported', 'rationale': 'Ambiguous direction.'}
    claim['source_validation'] = deepcopy(original)
    response.update(decision='unsupported', relation_match=False, direction_match=False, certainty_match=False)
    client = SourceClient(response)
    result = reassess_source_claim(claim, client)
    assert result['decision'] == 'unsupported'
    assert result['source_review_audit']['original'] == original
    assert result['source_review_audit']['reassessment']['decision'] == 'unsupported'
    assert claim['source_validation'] == original and '_source_reassessment' not in claim
    assert client.payload['source_reassessment']['previous_rationale'] == 'Ambiguous direction.'


def test_corrected_claim_invalidates_paper_cache_and_replaces_score_without_adding_votes(tmp_path):
    from tests.external.test_trust_graph import fixtures, GroupClient, config
    from backend.stages.trust_graph.workflow import assess_group
    claims, packets, answers = fixtures(1, 1)
    client = GroupClient(answers)
    first, _, _ = assess_group(claims, packets, client, config(), tmp_path)
    claims[0]['repair_revision'] = 1
    second, _, _ = assess_group(claims, packets, client, config(), tmp_path)
    assert len(client.state['calls']) == 4
    assert first[0]['trust']['score'] == second[0]['trust']['score'] == 1
