"""Start and inspect local extraction with ontology grounding."""

import json
from dataclasses import replace
from threading import Thread
from uuid import UUID, uuid4

from fastapi import APIRouter, HTTPException, Query

from backend.evaluation.endpoint_verifier import POLICY as VERIFIER_POLICY, verify_single_stage_output
from backend.evaluation.extract_then_ground import POLICY, run_extract_then_ground
from backend.evaluation.single_stage import source_targets
from backend.evaluation.single_stage_sample import PDF_HASH, prepare_sample
from backend.ontology.catalog import catalog
from backend.shared.activity import display_error
from backend.shared.artifacts import save_json, save_rows
from backend.shared.cancellation import CancellationToken, PipelineStopped, cancellation_scope
from backend.shared.jobs import ACTIVE_STATUSES
from backend.shared.storage import file_hash, read_jsonl, utc_now


def create_router(workspace):
    root = workspace.ONTOLOGY_DIR.parent / 'analysis' / 'single_stage'
    lookup_dir = root.parent / 'ontology_full_releases'
    tokens = workspace._single_stage_tokens = {}
    router = APIRouter(prefix='/api/ontology/single-stage')

    def writable():
        if getattr(workspace, 'read_only', False) or getattr(workspace, 'READ_ONLY', False):
            raise HTTPException(403, 'Single-stage runs are available in the local workbench.')

    def state(path):
        job_file = path / 'run.json'
        if not job_file.is_file():
            raise HTTPException(404, 'Single-stage run not found.')
        job = json.loads(job_file.read_text(encoding='utf-8'))
        if job['status'] in ACTIVE_STATUSES and job['id'] not in tokens:
            job.update(status='interrupted', error='The backend restarted during this run.')
        return job

    def worker(path, pdf, settings, token, sample, verifier):
        requests = []

        def update(**values):
            with workspace._lock:
                current = state(path)
                if current['status'] == 'stopping' and values.get('status') == 'running':
                    values.pop('status')
                current.update(values)
                save_json(path / 'run.json', current)

        def log_request(metrics):
            requests.append({'at': utc_now(), **metrics})
            save_rows(path / 'llm_requests.jsonl', requests)

        try:
            with cancellation_scope(token):
                token.check()
                reference = catalog()
                client = workspace.OpenCodeClient(settings, task='extraction', on_request=log_request,
                                                  response_dir=path / 'model_responses')
                thinking_client = (
                    workspace.OpenCodeClient(
                        replace(settings, thinking={**settings.thinking, 'extraction': True}),
                        task='extraction',
                        on_request=log_request,
                        response_dir=path / 'model_responses',
                    )
                    if verifier
                    else None
                )
                update(status='running', catalog_signature=reference.signature, pdf_sha256=file_hash(pdf))
                targets = None
                if sample:
                    prepared = prepare_sample(pdf, with_abbreviations=True)
                    cases, targets = map(list, zip(*prepared))
                    save_json(path / 'sample.json', cases)
                run_extract_then_ground(pdf, reference, client, path, update, settings.extraction_min_output_budget,
                                        lookup_dir, targets=targets, thinking_client=thinking_client)
                token.check()
                if verifier:
                    verifier_client = workspace.OpenCodeClient(
                        replace(settings, thinking={**settings.thinking, 'extraction': False}),
                        task='extraction',
                        on_request=log_request,
                        response_dir=path / 'model_responses',
                    )
                    method_targets = source_targets(pdf, with_abbreviations=True) if sample else None
                    verify_single_stage_output(
                        path, reference, verifier_client, update, lookup_dir, method_targets=method_targets,
                    )
                    token.check()
            update(status='completed')
        except PipelineStopped:
            update(status='stopped')
        except Exception as error:
            update(status='failed', error=display_error(str(error)))
        finally:
            with workspace._lock:
                tokens.pop(path.name, None)

    @router.get('/latest/{pdf_id}')
    def latest(pdf_id: str):
        workspace._pdf_path(pdf_id)
        pointer = root / pdf_id / 'latest.json'
        with workspace._lock:
            run_id = json.loads(pointer.read_text(encoding='utf-8'))['id'] if pointer.is_file() else None
            return state(root / pdf_id / run_id) if run_id else None

    @router.get('/{pdf_id}/{run_id}')
    def result(pdf_id: str, run_id: UUID, offset: int = Query(0, ge=0)):
        workspace._pdf_path(pdf_id)
        path = root / pdf_id / run_id.hex
        with workspace._lock:
            job = state(path)
            claims = read_jsonl(path / 'claims.jsonl')
            if job.get('sample'):
                case_file = path / 'sample.json'
                cases = json.loads(case_file.read_text(encoding='utf-8')) if case_file.is_file() else []
                return {**job, 'cases': [
                    {**case, 'claims': [row for row in claims if row['target_id'] == case['id']]}
                    for case in cases]}
            if job.get('comparison'):
                case_file = path / 'sample.json'
                cases = json.loads(case_file.read_text(encoding='utf-8')) if case_file.is_file() else []
                baseline = read_jsonl(path / 'two_stage_claims.jsonl')
                return {**job, 'claims': claims[offset:offset + 20], 'total': len(claims), 'limit': 20, 'cases': [
                    {**case, 'single': [r for r in claims if r['target_id'] == case['id']],
                     'two_stage': [r for r in baseline if r['target_id'] == case['id']]}
                    for case in cases]}
            visible_claims = claims[offset:offset + 20]
            return {**job, 'claims': visible_claims, 'total': len(claims), 'limit': 20}

    @router.post('/{pdf_id}', status_code=202)
    def start(pdf_id: str, sample: bool = False, comparison: bool = False, verifier: bool = False):
        writable()
        if comparison:
            raise HTTPException(422, 'Use the eight-passage test with sample=true.')
        pdf = workspace._pdf_path(pdf_id)
        if sample and file_hash(pdf) != PDF_HASH:
            raise HTTPException(422, 'Select the Kirchgesser PDF for the eight-passage test.')
        if not (lookup_dir / 'inventory.json').is_file():
            raise HTTPException(409, 'The local full ontology releases are unavailable.')
        settings = workspace.Settings()
        settings = replace(settings, thinking={**settings.task_thinking, 'extraction': False},
                           json_repair_attempts=0, json_regeneration_attempts=0)
        with workspace._lock:
            if (tokens or getattr(workspace, '_grounding_tester_tokens', {})
                    or any(job.get('status') in ACTIVE_STATUSES for job in workspace._jobs.values())):
                raise HTTPException(409, 'Finish or stop the active run first.')
            run_id = uuid4().hex
            path = root / pdf_id / run_id
            path.mkdir(parents=True)
            job = {'id': run_id, 'status': 'queued', 'model': settings.extraction_model,
                   'thinking': settings.task_thinking.get('extraction', False), 'workflow': POLICY,
                   'verifier': verifier, 'verifier_policy': VERIFIER_POLICY if verifier else None,
                   'selective_thinking': verifier,
                   'sample': sample, 'created_at': utc_now(), 'total_targets': 0,
                   'completed_targets': 0, 'claim_count': 0}
            save_json(path / 'run.json', job)
            save_json(path.parent / 'latest.json', {'id': run_id})
            token = tokens[run_id] = CancellationToken()
            Thread(target=worker, args=(path, pdf, settings, token, sample, verifier),
                   daemon=True, name='single-stage').start()
        return job

    @router.post('/{pdf_id}/{run_id}/stop')
    def stop(pdf_id: str, run_id: UUID):
        writable()
        workspace._pdf_path(pdf_id)
        path = root / pdf_id / run_id.hex
        with workspace._lock:
            job = state(path)
            if job['status'] in {'queued', 'running'} and (token := tokens.get(run_id.hex)):
                token.request()
                job['status'] = 'stopping'
                save_json(path / 'run.json', job)
            return job

    return router
