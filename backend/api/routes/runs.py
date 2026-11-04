"""Runs routes, with application state injected for shared jobs and saved views."""

from __future__ import annotations
from fastapi import APIRouter
from backend.api.contracts import RunOptions, SourceReviewOptions
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from fastapi import HTTPException
from backend.shared.storage import read_jsonl
from backend.shared.activity import display_error
from backend.shared.jobs import ACTIVE_STATUSES, read_job
from backend.shared.cancellation import CancellationToken


class RunsRoutes:
    def __init__(self, workspace):
        self.workspace = workspace

    def start_run(self, pdf_id: str, options: RunOptions) -> dict[str, Any]:
        return self.workspace._start_document_run(pdf_id, options)

    def resume_run(self, pdf_id: str, options: RunOptions) -> dict[str, Any]:
        return self.workspace._start_document_run(pdf_id, options, resume=True)

    def redo_external_validation(self, pdf_id: str, options: RunOptions) -> dict[str, Any]:
        return self.workspace._start_document_run(pdf_id, options, resume=True, redo_external=True)

    def recovery_options(self, pdf_id: str) -> dict[str, Any]:
        """Check saved progress for a user choice; never queue work from this GET."""
        pdf = self.workspace._pdf_path(pdf_id)
        output = self.workspace._output_dir(pdf)
        state = self.workspace._read_json(output / 'run_state.json', {})
        state = state if isinstance(state, dict) else {}
        saved = state.get('options') or {}
        saved = saved if isinstance(saved, dict) else {}
        options = {key: value for key, value in saved.items() if key in RunOptions.model_fields}
        if 'articles_per_claim' in saved and 'articles_per_pathway' not in options:
            options['articles_per_pathway'] = saved['articles_per_claim']
        reason, can_continue = None, False
        try:
            checkpoint = self.workspace.read_metadata(output) or {}
            options.update(self.workspace._saved_extraction_options(output))
            for key in ('max_pages', 'claims_per_packet'):
                if checkpoint.get(key) is not None:
                    options[key] = checkpoint[key]
            # Historical runs may predate saved options and checkpoint metadata.
            if not checkpoint and 'max_pages' not in options:
                quality = self.workspace._read_json(output / 'pdf_quality.json', {})
                selected, total = quality.get('selected_pages'), quality.get('total_pages')
                if isinstance(selected, int) and isinstance(total, int) and selected < total:
                    options['max_pages'] = selected
            restored_options = RunOptions(**options)
            options = restored_options.model_dump()
            with self.workspace._lock:
                active = any(job.get('pdf_id') == pdf_id and job.get('status') in ACTIVE_STATUSES
                             for job in self.workspace._jobs.values())
            if active:
                reason = 'This paper already has an active run.'
            else:
                meta = self.workspace.resume_metadata(output, pdf, replace(self.workspace.Settings(), thinking=restored_options.thinking.model_dump()), restored_options.max_pages,
                                restored_options.claims_per_packet, extraction_mode=restored_options.extraction_mode,
                                target_words=restored_options.extraction_target_words)
                can_continue = True
                if meta and meta.get('partial_extraction'):
                    reason = f"Saved extraction progress available: {meta.get('completed_targets', 0)} of {meta.get('total_targets', '?')} targets completed. Resume will continue remaining targets."
                elif (output / 'source_validation.jsonl').is_file() and (output / 'candidates.jsonl').is_file():
                    source_rows = self.workspace.read_jsonl(output / 'source_validation.jsonl')
                    cands = self.workspace.read_jsonl(output / 'candidates.jsonl')
                    if len(source_rows) < len(cands):
                        reason = f"Saved source validation progress available: {len(source_rows)} of {len(cands)} claims verified. Resume will continue remaining claims."
                if not reason and (output.parent / '.neighbor_validation').exists():
                    from backend.stages.neighbor_validation.records import assessment_view
                    view = assessment_view(output.parent, output.name)
                    completed_count = sum(v for k, v in view['counts'].items() if k not in (None, 'queued', 'in_progress', 'stale'))
                    if completed_count > 0:
                        reason = f"Saved pathway validation progress available: {completed_count} neighbor pairs verified. Resume will continue remaining connections."
        except (ValueError, OSError) as exc:
            reason = str(exc)
        return {'can_continue': can_continue, 'reason': reason, 'options': options,
                'job_id': state.get('id'), 'error': display_error(state.get('error')),
                'job_missing': not state.get('id') or (
                    state['id'] not in self.workspace._jobs and read_job(output, state['id']) is None),
                'interrupted': bool(state.get('interrupted')) or state.get('status') in ACTIVE_STATUSES}

    def _saved_extraction_options(self, output: Path) -> dict[str, Any]:
        metadata = self.workspace.read_metadata(output) or self.workspace._read_json(output / 'summary.json', {}) or {}
        defaults = self.workspace.Settings()
        # Runs predating thinking metadata used On. Preserve their extraction mode
        # on recovery even though fresh runs now default to Off.
        saved_thinking = metadata.get('thinking') or ({task: True for task in defaults.task_models} if metadata else defaults.task_thinking)
        restored = {'thinking': saved_thinking, 'extraction_mode': metadata.get('extraction_mode', 'whole_packet'),
                'neighbor_max_pairs': metadata.get('neighbor_max_pairs', metadata.get('pathway_max_paths',defaults.neighbor_max_pairs)),
                'extraction_target_words': metadata.get('extraction_target_words') or defaults.extraction_target_words,
                'extraction_generation_budget': metadata.get('extraction_generation_budget') or defaults.extraction_min_output_budget}
        for key in ('max_pages', 'claims_per_packet'):
            if metadata.get(key) is not None:
                restored[key] = metadata[key]
        return restored

    def _start_document_run(self, pdf_id: str, options: RunOptions, resume: bool = False, *, redo_external=False) -> dict[str, Any]:
        pdf = self.workspace._pdf_path(pdf_id)
        if resume:
            try:
                # An omitted new option on a historical resume must not switch its
                # extraction policy to the new default or reinterpret saved work.
                restored = {key: value for key, value in self.workspace._saved_extraction_options(self.workspace._output_dir(pdf)).items()
                            if key not in options.model_fields_set}
                options = RunOptions(**{**options.model_dump(), **restored})
                metadata = self.workspace.resume_metadata(self.workspace._output_dir(pdf), pdf, replace(self.workspace.Settings(), thinking=options.thinking.model_dump()), options.max_pages, options.claims_per_packet,
                                extraction_mode=options.extraction_mode, target_words=options.extraction_target_words)
                if redo_external and not metadata.get('extraction_complete'):
                    raise ValueError('Complete extraction before redoing external validation')
            except (ValueError, OSError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
        try:
            settings = self.workspace.Settings()
            available_models = set(self.workspace.OpenCodeClient(settings).health_check().get("models", []))
        except Exception as exc:
            raise HTTPException(status_code=503, detail=f"OpenCode is unavailable: {exc}") from exc
        missing = [f'{task}: {model}' for task, model in settings.task_models.items()
                   if model.split('#', 1)[0] not in available_models]
        if settings.external_paper_model not in available_models:
            missing.append(f'per-paper external: {settings.external_paper_model}')
        if missing:
            raise HTTPException(status_code=400, detail=f"Required OpenCode model is unavailable: {', '.join(missing)}")
        with self.workspace._lock:
            if any(job["status"] in ACTIVE_STATUSES for job in self.workspace._jobs.values()):
                raise HTTPException(status_code=409, detail="A pipeline is already running. Wait for it to finish.")
            job_id = uuid.uuid4().hex
            output_dir = self.workspace._output_dir(pdf)
            assessment_id = None
            if redo_external:
                assessment_id = job_id
            elif resume:
                assessment_id = ((self.workspace._read_json(output_dir / 'run_state.json', {}) or {}).get('external_assessment_id')
                                 or (self.workspace.read_metadata(output_dir) or {}).get('external_assessment_id'))
            job = {"id": job_id, "pdf_id": pdf_id, "pdf_path": str(pdf), "status": "queued", "resume": resume,
                   'redo_external_validation': redo_external, 'external_assessment_id': assessment_id,
                   "logs": read_jsonl(self.workspace._output_dir(pdf) / 'run_log.jsonl') if resume else [],
                   "options": options.model_dump(), "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                   "updated_at": None, "finished_at": None, "result": None, "error": None}
            output_dir.mkdir(parents=True, exist_ok=True)
            if not resume:
                from backend.shared.history import archive_run
                archive_run(output_dir)
            filenames = () if resume else ("summary.json", "document_scope.json", "graph.json", "source_graph.json", "audit_graph.json", "knowledge_graph.graphml", "reasoning_paths.json", "candidates.jsonl",
                             "extraction_rejections.jsonl", "schema_valid_candidates.jsonl", "semantic_validation.jsonl", "semantic_valid_candidates.jsonl",
                             "source_validation.jsonl", "source_entailed_candidates.jsonl",
                             "validation_runs.jsonl", "evaluation_sample.jsonl", "raw_triplets.txt", "llm_requests.jsonl", "extraction_packets.jsonl", "run_state.json", "claim_revisions.jsonl",
                             "pipeline_checkpoint.json", "query_translations.jsonl", "extraction_contexts.jsonl", "source_review_history.jsonl",
                             "pathway_coverage.json", "pathway_validation_summary.json", "external_validation_target.json")
            for filename in filenames:
                (output_dir / filename).unlink(missing_ok=True)
            log_path = output_dir / "run_log.jsonl"
            if not resume:
                log_path.write_text("", encoding="utf-8")
            self.workspace._jobs[job_id] = job
            self.workspace._persist_job(job_id)
        if resume:
            try:
                self.workspace.refresh_saved_views(pdf, output_dir)
            except Exception:
                pass
        threading.Thread(target=self.workspace._worker, args=(job_id, options), daemon=True, name=f"pdf-run-{pdf_id}").start()
        return self.workspace._job_snapshot(job)

    def review_source(self, pdf_id: str, candidate_id: str, options: SourceReviewOptions) -> dict[str, Any]:
        pdf = self.workspace._pdf_path(pdf_id)
        with self.workspace._lock:
            if any(job['status'] in ACTIVE_STATUSES for job in self.workspace._jobs.values()):
                raise HTTPException(status_code=409, detail='Stop or finish the active pipeline before saving a manual source review')
            try:
                return self.workspace.save_manual_source_review(pdf, self.workspace._output_dir(pdf), candidate_id, options.dimension, options.decision,
                    expected_revision=options.expected_revision, note=options.note)
            except LookupError as exc:
                raise HTTPException(status_code=404, detail=str(exc)) from exc
            except ValueError as exc:
                raise HTTPException(status_code=409 if 'Refresh' in str(exc) else 400, detail=str(exc)) from exc

    def start_claim_action(self, pdf_id: str, candidate_id: str, action: str, options: RunOptions) -> dict[str, Any]:
        """Queue a deliberate, single-claim operation without clearing PDF artifacts."""
        if action not in {'repair','reextract'}:
            raise HTTPException(status_code=410, detail='Individual-claim external validation is retired. Resume pathway validation instead.')
        pdf = self.workspace._pdf_path(pdf_id)
        output = self.workspace._output_dir(pdf)
        with self.workspace._lock:
            if any(job['status'] in ACTIVE_STATUSES for job in self.workspace._jobs.values()):
                raise HTTPException(status_code=409, detail='Wait for the current pipeline or claim action to finish')
            candidate = next((c for c in read_jsonl(output / 'candidates.jsonl') if c['candidate_id'] == candidate_id), None)
            if candidate is None:
                raise HTTPException(status_code=404, detail='Claim not found')
            validation = next((r for r in read_jsonl(output / 'validation_runs.jsonl') if r['candidate_id'] == candidate_id), None)
            if not self.workspace.repairable(candidate, validation):
                raise HTTPException(status_code=400, detail='Repair requires source review/correction or a verified external contradiction')
            job_id = uuid.uuid4().hex
            job = {'id': job_id, 'pdf_id': pdf_id, 'pdf_path': str(pdf), 'candidate_id': candidate_id,
                   'action': action, 'status': 'queued', 'logs': read_jsonl(output / 'run_log.jsonl'),
                   'options': options.model_dump(), 'started_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
                   'updated_at': None, 'finished_at': None, 'result': None, 'error': None}
            self.workspace._jobs[job_id] = job
            self.workspace._persist_job(job_id)
        threading.Thread(target=self.workspace._worker, args=(job_id, options), daemon=True, name=f'claim-{candidate_id}').start()
        return self.workspace._job_snapshot(job)

    def stop_job(self, job_id: str):
        with self.workspace._lock:
            job = self.workspace._jobs.get(job_id) or self.workspace._saved_job(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail='Job not found')
            if job.get('status') not in ACTIVE_STATUSES:
                return self.workspace._job_snapshot(job)
            if not job.get('stop_requested'):
                job.update(status='stopping', stop_requested=True)
                self.workspace._cancellations.setdefault(job_id, CancellationToken()).request()
                self.workspace._emit(job_id, 'info', 'Stop requested. Cancelling local model waits and waiting for active local operations to release. Saved checkpoints are preserved.')
            return self.workspace._job_snapshot(job)

    def job_status(self, job_id: str) -> dict[str, Any]:
        with self.workspace._lock:
            job = self.workspace._jobs.get(job_id) or self.workspace._saved_job(job_id)
            if job is None:
                raise HTTPException(status_code=404, detail="Job not found")
            return self.workspace._job_snapshot(job)

    def start_pathway_assembly(self, pdf_id: str, options: RunOptions | None = None):
        pdf = self.workspace._pdf_path(pdf_id)
        with self.workspace._lock:
            if any(job['status'] in ACTIVE_STATUSES for job in self.workspace._jobs.values()):
                raise HTTPException(status_code=409, detail='Wait for the current pipeline to finish')
            job_id = uuid.uuid4().hex
            options = options or RunOptions()
            job = dict(id=job_id, pdf_id=pdf_id, pdf_path=str(pdf), status='queued', action='assemble_pathways',
                       logs=read_jsonl(self.workspace._output_dir(pdf) / 'run_log.jsonl'), options=options.model_dump(),
                       started_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                       updated_at=None, finished_at=None, result=None, error=None)
            self.workspace._jobs[job_id] = job
            self.workspace._output_dir(pdf).mkdir(parents=True, exist_ok=True)
            self.workspace._persist_job(job_id)
        threading.Thread(target=self.workspace._worker, args=(job_id, options), daemon=True, name='global-pathway-assembly').start()
        return self.workspace._job_snapshot(job)


def create_router(workspace):
    views = RunsRoutes(workspace)
    router = APIRouter()
    router.add_api_route('/api/pdfs/{pdf_id}/run', views.start_run, methods=['POST'], status_code=202)
    router.add_api_route('/api/pdfs/{pdf_id}/resume', views.resume_run, methods=['POST'], status_code=202)
    router.add_api_route('/api/pdfs/{pdf_id}/redo-external-validation', views.redo_external_validation, methods=['POST'], status_code=202)
    router.add_api_route('/api/pdfs/{pdf_id}/recovery', views.recovery_options, methods=['GET'])
    router.add_api_route('/api/pdfs/{pdf_id}/claims/{candidate_id}/source-review', views.review_source, methods=['POST'])
    router.add_api_route('/api/pdfs/{pdf_id}/claims/{candidate_id}/{action}', views.start_claim_action, methods=['POST'], status_code=202)
    router.add_api_route('/api/jobs/{job_id}/stop', views.stop_job, methods=['POST'], status_code=202)
    router.add_api_route('/api/jobs/{job_id}', views.job_status, methods=['GET'])
    router.add_api_route('/api/pdfs/{pdf_id}/assemble-pathways', views.start_pathway_assembly, methods=['POST'], status_code=202)
    return router, views
