"""Small, isolated grounding experiments using the production grounding call."""
from dataclasses import replace
from collections import Counter
import json
from pathlib import Path
from threading import RLock, Thread
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field

from backend.config.settings import Settings
from backend.ontology.catalog import catalog
from backend.ontology.signatures import claim_signature
from backend.providers.llm import OpenCodeClient, format_request_metrics, request_log_level
from backend.shared.artifacts import save_json, save_rows
from backend.shared.cancellation import CancellationToken, PipelineStopped, cancellation_scope
from backend.shared.jobs import ACTIVE_STATUSES
from backend.shared.storage import read_jsonl, utc_now
from backend.stages.evaluation.grounding_pilot import run_pilot
from backend.domain.abbreviations import collect_definitions, relevant_definitions


class TestOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    pdf_id: str
    claim_ids: list[str] = Field(min_length=1, max_length=12)
    name: str = Field(default='Grounding examples', min_length=1, max_length=80)
    thinking: bool = True


class RepeatOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    thinking: bool = True


class TestReview(BaseModel):
    model_config = ConfigDict(extra='forbid')
    decision: Literal['acceptable', 'needs_review', 'not_reviewed']
    note: str = Field(default='', max_length=1500)


def mapping_coverage(claim, result, reference_signature):
    if result is None:
        return 'not_grounded'
    if (result.get('catalog_signature') != reference_signature
            or result.get('claim_signature') != claim_signature(claim)):
        return 'stale'
    exact = sum(result.get('bindings', {}).get(role, {}).get('mapping') == 'exact'
        and bool(result.get('bindings', {}).get(role, {}).get('iri')) for role in ('subject', 'relation', 'object'))
    return 'all_exact' if exact == 3 else 'some_exact' if exact else 'no_exact'


class GroundingTester:
    def __init__(self, workspace, *, client_factory=OpenCodeClient, launch=None):
        self.workspace, self.client_factory = workspace, client_factory
        self.root = workspace.ONTOLOGY_DIR.parent / 'analysis' / 'grounding_tester'
        self.lock, self.tokens = RLock(), {}
        workspace._grounding_tester_tokens = self.tokens
        self.launch = launch or (lambda fn: Thread(target=fn, daemon=True, name='grounding-tester').start())

    def directory(self, test_id):
        if len(test_id) != 32 or any(c not in '0123456789abcdef' for c in test_id):
            raise HTTPException(404, 'Test not found')
        path = self.root / test_id
        if not (path / 'test.json').exists():
            raise HTTPException(404, 'Test not found')
        return path

    def state(self, test_id):
        path = self.directory(test_id)
        data = json.loads((path / 'test.json').read_text(encoding='utf-8'))
        if data['status'] in ACTIVE_STATUSES and test_id not in self.tokens:
            data.update(status='interrupted', error='Backend restarted. Repeat this frozen example set; saved results remain available.')
        return data

    def source(self, pdf_id):
        pdf = self.workspace._pdf_path(pdf_id)
        directory = self.workspace._output_dir(pdf)
        if Path(directory.name).name != directory.name:
            raise HTTPException(422, 'Invalid saved document')
        return directory, [c for c in read_jsonl(directory / 'candidates.jsonl')
            if not c.get('schema_error') and not c.get('superseded_by')]

    def start(self, options, *, frozen=None, baseline=None):
        reference = catalog()
        with self.workspace._lock, self.lock:
            if any(j.get('status') in ACTIVE_STATUSES for j in self.workspace._jobs.values()) or self.tokens:
                raise HTTPException(409, 'Finish or stop the active run/test before starting a test')
            if frozen is None:
                directory, candidates = self.source(options.pdf_id)
                ids = options.claim_ids
                if len(set(ids)) != len(ids):
                    raise HTTPException(422, 'Select each claim once')
                by_id = {c['candidate_id']: c for c in candidates}
                if set(ids) - by_id.keys():
                    raise HTTPException(409, 'Selected claims changed; refresh the example list')
                glossary = collect_definitions(candidates)
                frozen = []
                for identifier in ids:
                    claim = dict(by_id[identifier])
                    claim['source_abbreviation_definitions'] = relevant_definitions(claim, glossary)
                    frozen.append({'document': directory.name, 'claim_id': identifier, 'role': 'subject',
                        'source': claim, 'catalog_signature': reference.signature,
                        'claim_signature': claim_signature(claim)})
                baseline = [r for r in read_jsonl(directory / 'ontology_grounding.jsonl') if r['candidate_id'] in ids]
            elif any(r['catalog_signature'] != reference.signature for r in frozen):
                raise HTTPException(409, 'The ontology reference changed. Select a new example set; old results are preserved.')
            test_id = uuid4().hex
            path = self.root / test_id
            path.mkdir(parents=True)
            settings = replace(Settings(), thinking={**Settings().task_thinking, 'pathway_assembly': options.thinking})
            state = {'id': test_id, 'name': options.name, 'pdf_id': options.pdf_id,
                'thinking': options.thinking, 'status': 'queued', 'created_at': utc_now(),
                'claim_count': len(frozen), 'catalog_signature': reference.signature,
                'model': settings.pathway_assembly_model, 'events': [], 'reviews': {},
                'isolated': True, 'error': None}
            save_rows(path / 'sample.jsonl', frozen)
            save_rows(path / 'baseline.jsonl', baseline or [])
            save_json(path / 'test.json', state)
            token = self.tokens[test_id] = CancellationToken()
        self.launch(lambda: self.run(test_id, reference, settings, token))
        return {'id': test_id, 'status': 'queued'}

    def run(self, test_id, reference, settings, token):
        path = self.directory(test_id)
        def update(**values):
            with self.lock:
                state = self.state(test_id)
                state.update(values)
                save_json(path / 'test.json', state)
        def emit(level, message):
            with self.lock:
                state = self.state(test_id)
                state['events'] = [*state['events'], {'at': utc_now(), 'level': level, 'message': message}][-200:]
                save_json(path / 'test.json', state)
        requests = []
        def log(metrics):
            with self.lock:
                requests.append({'at': utc_now(), **metrics})
                save_rows(path / 'llm_requests.jsonl', requests)
            emit(request_log_level(metrics), format_request_metrics(metrics))
        try:
            update(status='running')
            with cancellation_scope(token):
                token.check()
                client = self.client_factory(settings, task='pathway_assembly', on_request=log,
                    response_dir=path / 'model_responses')
                summary = run_pilot(read_jsonl(path / 'sample.jsonl'), reference, path / 'results', client, emit=emit)
            update(status='completed', summary=summary)
        except PipelineStopped as exc:
            update(status='stopped', error=str(exc))
        except Exception as exc:
            update(status='failed', error=str(exc))
        finally:
            update(finished_at=utc_now())
            with self.lock:
                self.tokens.pop(test_id, None)


def create_router(workspace, **kwargs):
    from backend.domain.abbreviations import collect_definitions, expand_claim_names
    service = GroundingTester(workspace, **kwargs)
    router = APIRouter(prefix='/api/ontology/tester')

    @router.get('/claims')
    def claims(pdf_id: str, q: str = '',
               coverage: Literal['all', 'no_exact', 'some_exact', 'all_exact', 'not_grounded', 'stale'] = 'all',
               offset: int = Query(0, ge=0), limit: int = Query(20, ge=1, le=50)):
        directory, rows = service.source(pdf_id)
        glossary = collect_definitions(rows)
        reference_signature = catalog().signature
        mappings = {r['candidate_id']: r for r in read_jsonl(directory / 'ontology_grounding.jsonl')}
        rows = [r for r in rows if not q or q.casefold() in ' '.join(str(r.get(k) or '') for k in
            ('subject', 'relation', 'object', 'qualifiers', 'source_evidence_span')).casefold()]
        groups = {r['candidate_id']: mapping_coverage(r, mappings.get(r['candidate_id']), reference_signature) for r in rows}
        counts = Counter({k: 0 for k in ('no_exact', 'some_exact', 'all_exact', 'not_grounded', 'stale')})
        counts.update(groups.values())
        counts['all'] = len(rows)
        rows = [r for r in rows if coverage == 'all' or groups[r['candidate_id']] == coverage]
        displayed = [expand_claim_names(r, glossary) for r in rows[offset:offset + limit]]
        return {'claims': [{k: r.get(k) for k in ('candidate_id', 'subject', 'relation', 'object',
            'source_evidence_span', 'source_page')} for r in displayed],
            'total': len(rows), 'offset': offset, 'limit': limit, 'coverage_counts': counts}

    @router.get('/tests')
    def tests():
        with service.lock:
            rows = [service.state(p.parent.name) for p in service.root.glob('*/test.json')]
        return {'tests': sorted(rows, key=lambda r: r['created_at'], reverse=True)[:50]}

    @router.post('/tests', status_code=202)
    def start(options: TestOptions):
        return service.start(options)

    @router.get('/tests/{test_id}')
    def detail(test_id: str):
        with service.lock:
            state = service.state(test_id)
            path = service.directory(test_id)
            rows = [r for p in (path / 'results').glob('*/ontology_grounding.jsonl') for r in read_jsonl(p)]
            baseline = read_jsonl(path / 'baseline.jsonl')
            glossary = collect_definitions([r['original_claim'] for r in rows + baseline])
            def display(records):
                return [{**r, 'original_claim': expand_claim_names(r['original_claim'], glossary)} for r in records]
            return {**state, 'results': display(rows), 'baseline': display(baseline),
                'sample': read_jsonl(path / 'sample.jsonl'),
                'current_reference': state['catalog_signature'] == catalog().signature}

    @router.post('/tests/{test_id}/stop')
    def stop(test_id: str):
        with service.lock:
            service.directory(test_id)
            if test_id in service.tokens:
                service.tokens[test_id].request('Tester stopped locally; remote cancellation is unconfirmed.')
            return {'message': 'Stop requested; completed test results are preserved.'}

    @router.post('/tests/{test_id}/repeat', status_code=202)
    def repeat(test_id: str, options: RepeatOptions):
        with service.lock:
            state = service.state(test_id)
            path = service.directory(test_id)
            sample = read_jsonl(path / 'sample.jsonl')
            baseline = [r for p in (path / 'results').glob('*/ontology_grounding.jsonl') for r in read_jsonl(p)]
        return service.start(TestOptions(pdf_id=state['pdf_id'], name=state['name'], thinking=options.thinking,
            claim_ids=[r['claim_id'] for r in sample]), frozen=sample, baseline=baseline)

    @router.post('/tests/{test_id}/review/{claim_id}')
    def review(test_id: str, claim_id: str, options: TestReview):
        with service.lock:
            path = service.directory(test_id)
            state = service.state(test_id)
            if state['status'] in ACTIVE_STATUSES:
                raise HTTPException(409, 'Wait for this test to finish before reviewing')
            if claim_id not in {r['claim_id'] for r in read_jsonl(path / 'sample.jsonl')}:
                raise HTTPException(404, 'Claim not in this test')
            state['reviews'][claim_id] = {**options.model_dump(), 'at': utc_now()}
            save_json(path / 'test.json', state)
            return state['reviews'][claim_id]

    return router
