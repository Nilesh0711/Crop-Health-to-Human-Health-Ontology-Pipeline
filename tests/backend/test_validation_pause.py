"""Paused stages cannot spend credits, gate assembly, or create scientific support."""
import importlib
import json
from dataclasses import replace

import pytest
from fastapi import HTTPException

from backend.api.contracts import RunOptions
from backend.api.routes.runs import RunsRoutes
from backend.config.pipeline import active_task_models
from backend.config.settings import Settings
from backend.shared.artifacts import save_rows
from backend.shared.storage import read_jsonl, file_hash
from backend.stages.pathway_assembly.assembly import assemble_pathways, read_assembly
from backend.stages.pathway_assembly.corpus import collect_corpus
from tests.backend.test_pathway_assembly import Aligner


def claim(identifier='one', **extra):
    return {'candidate_id': identifier, 'subject': 'Compound T', 'relation': 'affects',
            'object': 'Process P', 'subject_type': 'Unresolved', 'object_type': 'PlantProcess',
            'schema_error': None, 'certainty': 'conditional', 'qualifiers': {},
            'source_evidence_span': 'Compound T may affect Process P.', 'source_page': 1,
            'source_evidence_sentence_numbers': [1], **extra}


def test_unvalidated_corpus_does_not_convert_historical_holds_into_support(tmp_path):
    output = tmp_path / 'paper'
    output.mkdir()
    save_rows(output / 'candidates.jsonl', [claim(), claim('held', source_validation={
        'decision': 'needs_review', 'project_scope': {'decision': 'out_of_scope'}}),
        claim('invalid', schema_error='invalid sentence reference'),
        claim('parent', superseded_by=['child'])])
    original = (output / 'candidates.jsonl').read_bytes()
    assert collect_corpus(tmp_path, provisional=True)[0] == []
    edges, endpoints = collect_corpus(tmp_path, provisional=True, unvalidated=True)
    assert len(edges) == 2 and len(endpoints) == 4
    assert all(e['status'] == 'unvalidated' and e['source_status'] == 'not_assessed' for e in edges)
    assert all(e['project_scope']['decision'] == 'not_assessed' and e['support_count'] == 0 for e in edges)
    assert (output / 'candidates.jsonl').read_bytes() == original
    with pytest.raises(ValueError, match='provisional'):
        collect_corpus(tmp_path, unvalidated=True)


def test_new_assembly_is_isolated_and_resumes_without_validation(tmp_path):
    output = tmp_path / 'paper'
    output.mkdir()
    save_rows(output / 'candidates.jsonl', [claim()])
    old = tmp_path / '.provisional_pathways'
    old.mkdir()
    (old / 'snapshot.json').write_text('{"historical": true}', encoding='utf-8')
    settings = replace(Settings(), pathway_candidate_strategy='exhaustive')
    aligner = Aligner()
    first = assemble_pathways(tmp_path, aligner, settings, provisional=True, unvalidated=True)
    calls = len(aligner.calls)
    second = assemble_pathways(tmp_path, aligner, settings, provisional=True, unvalidated=True)
    assert len(aligner.calls) == calls and first == second
    assert first['validation_stages_paused'] is True
    assert (old / 'snapshot.json').read_text() == '{"historical": true}'
    view = read_assembly(tmp_path, settings.pathway_assembly_model, provisional=True, unvalidated=True)
    assert not view['stale'] and len(view['snapshot']['graph']['edges']) == 1


@pytest.mark.parametrize('action', ['redo', 'review', 'repair', 'private_redo'])
def test_archived_actions_are_blocked_before_job_or_provider_access(action):
    routes = RunsRoutes(object())
    with pytest.raises(HTTPException) as failure:
        if action == 'redo':
            routes.redo_external_validation('missing', RunOptions())
        elif action == 'review':
            routes.review_source('missing', 'one', None)
        elif action == 'repair':
            routes.start_claim_action('missing', 'one', 'repair', RunOptions())
        else:
            routes._start_document_run('missing', RunOptions(), redo_external=True)
    assert failure.value.status_code == 409 and 'archived' in failure.value.detail


def test_health_requires_only_active_models(monkeypatch):
    app = importlib.import_module('backend.api.app')
    class Client:
        def __init__(self, settings):
            self.settings = settings
        def health_check(self):
            return {'models': list(active_task_models(self.settings).values())}
    monkeypatch.setattr(app, 'OpenCodeClient', Client)
    health = app.health()
    assert health['model_ready']
    assert set(health['pipeline']['task_models']) == {'extraction'}
    assert health['pipeline']['stage_status']['ontology_grounding'] == 'manual'
    assert health['pipeline']['manual_grounding']['enabled']
    assert health['validation']['enabled'] is False
    assert health['pipeline']['stage_status']['project_scope'] == 'paused'


def test_ground_saved_claims_action_is_blocked_before_touching_workspace(monkeypatch):
    routes = importlib.import_module('backend.api.routes.runs')
    monkeypatch.setattr(routes, 'MANUAL_GROUNDING_ENABLED', False)
    with pytest.raises(HTTPException) as error:
        RunsRoutes(object()).start_pathway_assembly('paper')
    assert error.value.status_code == 409
    assert 'grounding is paused' in error.value.detail


@pytest.mark.parametrize('grounding_paused', [True, False])
def test_document_run_and_resume_never_create_validation_clients(tmp_path, monkeypatch, grounding_paused):
    from tests.backend.test_ontology_grounding import Grounder
    module = importlib.import_module('backend.pipelines.document')
    policy = importlib.import_module('backend.config.pipeline')
    monkeypatch.setattr(module, 'ONTOLOGY_GROUNDING_PAUSED', grounding_paused)
    monkeypatch.setattr(policy, 'ONTOLOGY_GROUNDING_PAUSED', grounding_paused)
    root = tmp_path / 'ontology'
    pdf = tmp_path / 'paper.pdf'
    pdf.write_bytes(b'Offline PDF fixture')
    configured = replace(Settings(), pathway_candidate_strategy='exhaustive')
    tasks, extraction_calls, messages = [], [], []
    class Client(Grounder):
        def __init__(self, settings, model=None, task=None, **kwargs):
            super().__init__()
            self.model = model or 'offline-grounding-fixture'
            self.request_metrics = {'requests': 0, 'elapsed_seconds': 0}
            if task:
                tasks.append(task)
        def health_check(self):
            return {'models': list(active_task_models(configured).values())}
    inspection = {'regions': [], 'pages': [], 'total_pages': 1, 'selected_pages': 1,
                  'pages_with_text': 1, 'pages_with_warnings': 0, 'chapter_boundaries': []}
    def extract(*args, **kwargs):
        extraction_calls.append(True)
        kwargs['packet_results'].append({'packet_number': 1, 'status': 'completed'})
        extracted = claim(subject='phosphate', relation='increases', object='root length',
                          source_pdf_sha256=file_hash(pdf), source_document_id='paper')
        for key in ('subject_type', 'object_type', 'certainty'):
            extracted.pop(key, None)
        return [extracted], {}, []
    monkeypatch.setattr(module, 'ONTOLOGY_DIR', root)
    monkeypatch.setattr(module, 'Settings', lambda: configured)
    monkeypatch.setattr(module, 'OpenCodeClient', Client)
    monkeypatch.setattr(module, 'PdfReader', lambda *args: None)
    monkeypatch.setattr(module, 'inspect_pdf', lambda *args: inspection)
    monkeypatch.setattr(module, 'iter_pdf_context_packets', lambda *args, **kwargs: [])
    monkeypatch.setattr(module, 'source_article_identifiers', lambda *args: {})
    monkeypatch.setattr(module, 'pdf_bibliography', lambda *args, **kwargs: [])
    monkeypatch.setattr(module, 'assess_text_quality', lambda *args: {'detected_quality': 'usable'})
    monkeypatch.setattr(module, 'extract_claims', extract)
    monkeypatch.setattr(module, 'refresh_candidate_citations', lambda candidates, *args: candidates)
    emit = lambda level, message: messages.append(str(message))
    first = module.run_pipeline(pdf, 0, 8, emit)
    output = root / 'paper'
    # A prior assessment remains auditable, but must not gate or validate a resume.
    rows = read_jsonl(output / 'candidates.jsonl')
    rows[0]['source_validation'] = {'decision': 'needs_review'}
    save_rows(output / 'candidates.jsonl', rows)
    save_rows(output / 'source_validation.jsonl', [{'historical': True}])
    historical = (output / 'source_validation.jsonl').read_bytes()
    if grounding_paused:
        save_rows(output / 'ontology_grounding.jsonl', [{'historical': True}])
        previous_grounding = (output / 'ontology_grounding.jsonl').read_bytes()
    second = module.run_pipeline(pdf, 0, 8, emit, resume=True)
    assert set(tasks) == ({'extraction'} if grounding_paused else {'extraction', 'pathway_assembly'})
    assert len(extraction_calls) == 1
    assert first['source_entailed'] == second['source_entailed'] == 0
    assert second['externally_evaluated'] == 0 and second['graph']['edges'] == 0
    assert second['stage_status']['external_validation'] == 'paused'
    assert second['stage_status']['reasoning'] == 'paused'
    assert second['pathway_assembly']['status'] == 'paused'
    if grounding_paused:
        assert second['ontology_grounding'] == {'status': 'not_run', 'assessed': False,
            'claims': 0, 'mapped_claims': 0, 'not_assessed': 1}
        assert 'ontology_grounding' not in second['stage_timings_seconds']
        assert (output / 'ontology_grounding.jsonl').read_bytes() == previous_grounding
    else:
        assert second['ontology_grounding']['mapped_claims'] == 1
        assert 'ontology_grounding' in second['stage_timings_seconds']
    assert 'graph_building' not in second['stage_timings_seconds']
    assert not (root / '.neighbor_validation').exists()
    assert not (output / 'validation_runs.jsonl').exists()
    assert (output / 'source_validation.jsonl').read_bytes() == historical
    assert not (output / 'graph.json').exists()
    assert not (output / 'knowledge_graph.graphml').exists()
    assert not (output / 'evaluation_sample.jsonl').exists()
    assert not (root / '.ontology_grounding' / 'reasoning.json').exists()
    assert all('Checking source fidelity' not in message for message in messages)
