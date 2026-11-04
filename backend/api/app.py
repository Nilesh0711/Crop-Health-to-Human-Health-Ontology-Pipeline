"""FastAPI server for the local crop to human health workbench."""

from __future__ import annotations

import json
import sys
import re
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.middleware.gzip import GZipMiddleware
# Re-export request contracts for existing integrations.
from backend.api.contracts import ThinkingOptions, RunOptions, SourceReviewOptions

from backend.config.settings import ONTOLOGY_DIR, PDF_DIR, SCHEMA_VERSION, APPLICATION_VERSION, Settings
from backend.config.paths import FRONTEND_DIST_DIR
from backend.providers.llm import OpenCodeClient, model_reasoning_policy
from backend.stages.external_validation.history import verified_contradictions
from backend.shared.storage import read_jsonl, stable_hash
from backend.shared.artifacts import ARTIFACT_WRITE_POLICY

from backend.pipelines.document import run_pipeline
from backend.pipelines.checkpoints import resume_available, resume_metadata, read_metadata
from backend.shared.activity import display_error, display_log, progress_message, activity_stage, research_activity
from backend.pipelines.claim_repair import repairable, run_claim_action, refresh_saved_views
from backend.shared.jobs import ACTIVE_STATUSES, save_job, read_job, interrupt_job
from backend.shared.cancellation import CancellationToken, PipelineStopped, cancellation_scope, check_cancelled
from backend.pipelines.pathways import assemble_saved_pathways
from backend.stages.pathway_assembly.assembly import read_assembly
from backend.stages.source_validation.abbreviations import SOURCE_VALIDATION_POLICY
from backend.stages.source_validation.summary import source_assessment_counts
from backend.pipelines.source_review import save_manual_source_review
from backend.stages.pathway_assembly.corpus import PROVISIONAL_ELIGIBILITY_POLICY


app = FastAPI(title="Crop to Human Health Workbench", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["*"])
app.add_middleware(GZipMiddleware, minimum_size=1000)


_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()
_cancellations: dict[str, CancellationToken] = {}


def _pdf_map() -> dict[str, Path]:
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    return {stable_hash(path.name, 16): path for path in sorted(PDF_DIR.glob("*.pdf")) if path.is_file()}


def _pdf_path(pdf_id: str) -> Path:
    path = _pdf_map().get(pdf_id)
    if path is None:
        raise HTTPException(status_code=404, detail="PDF not found")
    return path


def _output_dir(pdf: Path) -> Path:
    return ONTOLOGY_DIR / pdf.stem


def _read_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return default


def _job_snapshot(job: dict[str, Any]) -> dict[str, Any]:
    snapshot = {key: value for key, value in job.items() if key != "pdf_path"}
    snapshot['logs'] = [display_log(entry) for entry in job.get('logs', []) if research_activity(entry)]
    snapshot['error'] = display_error(job.get('error'))
    if isinstance(job.get('request_progress'), dict):
        snapshot['request_progress'] = display_log({'progress': job['request_progress']})['progress']
    return snapshot


def _persist_job(job_id: str) -> None:
    job = _jobs[job_id]
    save_job(_output_dir(Path(job['pdf_path'])), job_id, job)


def _saved_job(job_id: str) -> dict[str, Any] | None:
    """Recover a status snapshot, never restart a paid request during polling."""
    for pdf_id, pdf in _pdf_map().items():
        output = _output_dir(pdf)
        record = read_job(output, job_id)
        if record is None or record.get('pdf_id') != pdf_id:
            continue
        record['pdf_path'] = str(pdf)
        if record.get('status') in ACTIVE_STATUSES:
            entry = interrupt_job(record)
            latest = _read_json(output / 'run_state.json', {})
            is_latest = latest.get('id') == job_id
            if is_latest:
                with (output / 'run_log.jsonl').open('a', encoding='utf-8') as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + '\n')
            save_job(output, job_id, record, update_latest=is_latest)
        _jobs[job_id] = record
        return record
    return None


def _summary_with_coverage(output: Path) -> dict[str, Any] | None:
    """Annotate historical completion labels from saved call logs, without rewriting data."""
    summary = _read_json(output / 'summary.json')
    if (output / 'external_validation_target.json').exists() and (output.parent / '.neighbor_validation').exists():
        from backend.stages.neighbor_validation.records import assessment_view
        pathways = assessment_view(output.parent, output.name)
        summary = {**(summary or {}), 'knowledge_building_mode':'pathway_only', 'external_validation_target':'neighboring_claims',
            'externally_evaluated':0,'external_incomplete':0,
            **source_assessment_counts(read_jsonl(output / 'candidates.jsonl')),
            'pathway_validation':{'outcomes':pathways['counts'],'coverage':pathways['coverage']},
            'graph':_read_json(output / 'graph.json',{}).get('counts',{})}
    if not isinstance(summary, dict) or 'extraction_coverage' in summary:
        return summary
    packets: dict[int, dict[str, Any]] = {}
    total = 0
    for request in read_jsonl(output / 'llm_requests.jsonl'):
        match = re.fullmatch(r'extraction packet (\d+)/(\d+)', str(request.get('task') or ''))
        if not match:
            continue
        number, count = map(int, match.groups())
        total = max(total, count)
        packets[number] = {'packet_number': number,
                           'status': 'completed' if request.get('status') == 'ok' else request.get('status', 'failed'),
                           'error': f"Saved model request ended with status {request.get('status', 'unknown')}",
                           'page_start': None, 'page_end': None}
    if not packets:
        return summary
    failed = [row for row in packets.values() if row['status'] != 'completed']
    missing = [{'packet_number': number, 'status': 'not_recorded', 'error': 'No completed model request recorded',
                'page_start': None, 'page_end': None} for number in range(1, total + 1) if number not in packets]
    return {**summary, 'extraction_coverage': {'complete': not failed and not missing, 'total_packets': total,
                    'completed_packets': sum(row['status'] == 'completed' for row in packets.values()),
                    'failed_packets': failed + missing, 'inferred_from': 'llm_requests.jsonl'}}


def _emit(job_id: str, level: str, message: str) -> None:
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "level": level, "message": message}
    if level == 'progress':
        progress = json.loads(message)
        entry.update(progress=progress, message=progress_message(progress))
    explicit_stage = getattr(message, 'stage', None)
    if explicit_stage:
        entry['stage'] = explicit_stage
    if getattr(message, 'candidate_id', None):
        entry['candidate_id'] = message.candidate_id
    with _lock:
        job = _jobs[job_id]
        entry['stage'] = activity_stage(entry, job.get('activity_stage', 'system'))
        visible = research_activity(entry)
        if not visible and level != 'progress':
            return
        job['activity_stage'] = entry['stage']
        if level == 'progress':
            active = job.setdefault('active_request_progress', {})
            key = f"{progress.get('stage')}:{progress.get('model')}:{progress.get('request_id')}"
            if progress.get('status') in {'waiting_for_provider', 'repairing_format', 'regenerating_response'}:
                active[key] = progress
            else:
                active.pop(key, None)
            # A completed sibling must not hide another request still waiting.
            shown = next(iter(active.values()), progress)
            job['request_progress'] = {**shown, 'active_request_count': len(active)}
        if visible:
            job["logs"].append(entry)
        job["updated_at"] = entry["at"]
        if visible:
            log_path = _output_dir(Path(job["pdf_path"])) / "run_log.jsonl"
            log_path.parent.mkdir(parents=True, exist_ok=True)
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        _persist_job(job_id)


def _worker(job_id: str, options: RunOptions) -> None:
    with _lock:
        token = _cancellations.setdefault(job_id, CancellationToken())
    try:
        with cancellation_scope(token):
            _run_worker(job_id, options)
    finally:
        with _lock:
            _cancellations.pop(job_id, None)


def _run_worker(job_id: str, options: RunOptions) -> None:
    with _lock:
        if _jobs[job_id].get("status") != "stopping":
            _jobs[job_id]["status"] = "running"
        pdf = Path(_jobs[job_id]["pdf_path"])
        _persist_job(job_id)
    try:
        check_cancelled()
        label = ('Starting global Pathway Assembly from saved claims' if _jobs[job_id].get('action') == 'assemble_pathways' else
                 f"Starting {_jobs[job_id]['action']} for claim {_jobs[job_id]['candidate_id']} in {pdf.name}"
                 if _jobs[job_id].get('action') else f"Starting pipeline for {pdf.name}")
        if _jobs[job_id].get('resume'):
            label = f'Resuming pipeline from saved checkpoints for {pdf.name}'
        if _jobs[job_id].get('redo_external_validation'):
            label = f'Redoing external validation of all selected assembled pathways for {pdf.name}; saved extraction is retained'
        _emit(job_id, "stage", label)
        if _jobs[job_id].get('action') == 'assemble_pathways':
            result = assemble_saved_pathways(ONTOLOGY_DIR, _output_dir(pdf), lambda level,message:_emit(job_id, level, message), thinking=options.thinking.model_dump())
        elif _jobs[job_id].get('action'):
            result = run_claim_action(pdf, _output_dir(pdf), _jobs[job_id]['candidate_id'],
                                      _jobs[job_id]['action'], options.articles_per_pathway,
                                      lambda level, message: _emit(job_id, level, message), thinking=options.thinking.model_dump(),
                                      neighbor_max_pairs=options.neighbor_max_pairs)
        else:
            result = run_pipeline(pdf, options.max_pages, options.articles_per_pathway,
                                  lambda level, message: _emit(job_id, level, message),
                                  claims_per_packet=options.claims_per_packet,
                                  extraction_mode=options.extraction_mode,
                                  extraction_target_words=options.extraction_target_words,
                                  extraction_generation_budget=options.extraction_generation_budget,
                                  thinking=options.thinking.model_dump(),
                                  neighbor_max_pairs=options.neighbor_max_pairs,
                                  resume=bool(_jobs[job_id].get('resume')),
                                  **({'external_assessment_id': _jobs[job_id]['external_assessment_id']}
                                     if _jobs[job_id].get('external_assessment_id') else {}))
        with _lock:
            check_cancelled()
            _jobs[job_id]["status"] = result.get('run_status', 'completed')
            _jobs[job_id]["result"] = result
    except PipelineStopped:
        _emit(job_id, 'info', 'Pipeline stopped by user. Completed work is saved; resume when available. A remote provider may continue processing an already submitted request.')
        with _lock:
            job = _jobs[job_id]
            job.update(status='stopped', stop_requested=True, error=None)
            if isinstance(job.get('request_progress'), dict):
                job['request_progress'] = {**job['request_progress'], 'status':'cancelled',
                    'provider_status':'Stopped locally; remote provider cancellation is unconfirmed'}
        try:
            summary = refresh_saved_views(pdf, _output_dir(pdf))
            if summary:
                summary['run_status'] = 'stopped'
                from backend.shared.artifacts import save_json
                save_json(_output_dir(pdf) / 'summary.json', summary)
        except Exception as exc:
            _emit(job_id, 'warning', f'Stopped; could not refresh saved views: {exc}')
    except Exception as exc:
        _emit(job_id, "error", str(exc))
        with _lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)
        try:
            refresh_saved_views(pdf, _output_dir(pdf))
        except Exception as refresh_error:
            _emit(job_id, 'error', f'Could not refresh partial results: {refresh_error}')
    finally:
        with _lock:
            _jobs[job_id].pop('active_request_progress', None)
            if isinstance(_jobs[job_id].get('request_progress'), dict):
                _jobs[job_id]['request_progress']['active_request_count'] = 0
            _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
            _persist_job(job_id)


@app.get("/api/health")
def health() -> dict[str, Any]:
    settings = Settings()
    try:
        opencode_models = OpenCodeClient(settings).health_check().get("models", [])
    except Exception:
        opencode_models = []
    def route_ready(model: str, task: str) -> bool:
        return model.split('#', 1)[0] in opencode_models
    ready = {task: route_ready(model, task) for task, model in settings.task_models.items()}
    phase_ready = {phase: route_ready(model, 'external') for phase, model in settings.external_phase_models.items()}
    ready['external'] = all(phase_ready.values())
    extraction_ready, validation_ready, helper_ready = (ready[task] for task in ('extraction', 'external', 'query'))
    return {"status": "ok", "capabilities": {"execute": True, "upload": True, "read_only": False},
            "model": settings.external_model, "model_ready": all(ready.values()),
            "available_models": opencode_models,
            "extraction": {"provider": "opencode", "model": settings.extraction_model,
                           "ready": extraction_ready, "available_models": opencode_models},
            "validation": {"provider": "opencode", "model": settings.external_model,
                           "ready": validation_ready, "available_models": opencode_models},
            "helper": {"provider": "opencode", "model": settings.query_model, "ready": helper_ready},
            "pipeline": {"schema_version": SCHEMA_VERSION, "version": APPLICATION_VERSION,
                         "artifact_write_policy": ARTIFACT_WRITE_POLICY,
                         "pathway_assembly_policy": PROVISIONAL_ELIGIBILITY_POLICY,
                         "pathway_candidate_strategy": settings.pathway_candidate_strategy,
                         "pathway_candidate_top_k": settings.pathway_candidate_top_k,
                         "pathway_candidate_expansion_k": settings.pathway_candidate_expansion_k,
                         "pathway_candidate_small_registry": settings.pathway_candidate_small_registry,
                         "external_validation_target": 'neighboring_claims',
                         "external_validation_policy": 'neighbor-pair-v1',
                         "source_validation_policy": SOURCE_VALIDATION_POLICY,
                         "external_judgement_mode": "pathway_only",
                         "knowledge_building_mode": settings.knowledge_building_mode,
                         "neighbor_max_pairs": settings.neighbor_max_pairs,
                         "neighbor_workers":settings.neighbor_workers, "source_workers":settings.source_workers,
                         "validation_batch_size": settings.validation_batch_size,
                         "model_policy": "primary_only",
                         "task_models": settings.task_models,
                         "external_phase_models": settings.external_phase_models,
                         "external_phase_ready": phase_ready,
                         "external_top_chunks": settings.external_top_chunks,
                         "external_top_k": settings.external_top_k,
                         "external_input_max_characters": settings.external_input_max_characters,
                         "task_ready": ready,
                         "extraction_timeout_seconds": settings.extraction_timeout_seconds,
                         "extraction_mode": settings.extraction_mode,
                         "extraction_target_words": settings.extraction_target_words,
                         "extraction_generation_budget": settings.extraction_min_output_budget,
                         "extraction_claims_per_packet": settings.extraction_claims_per_packet,
                         "external_timeout_seconds": settings.external_timeout_seconds,
                         "external_min_output_budget": settings.external_min_output_budget,
                         "reasoning_policy": {task: model_reasoning_policy(model, settings.task_thinking[task]) for task, model in settings.task_models.items()},
                         "thinking": settings.task_thinking,
                         "retrieval_workers": settings.retrieval_workers,
                         "external_paper_workers": settings.external_paper_workers,
                         "evidence_index_enabled": settings.evidence_index_enabled,
                         "evidence_index_ttl_hours": settings.evidence_index_ttl_hours,
                         "search_wait_seconds": settings.search_wait_seconds,
                         "embedding_batch_size": settings.embedding_batch_size,
                         "cross_encoder_batch_size": settings.cross_encoder_batch_size,
                         "opencode_transport": 'api',
                         "response_format_policy": settings.opencode_response_format,
                         "json_repair_attempts": settings.json_repair_attempts,
                         "json_regeneration_attempts": settings.json_regeneration_attempts,
                         "recovery_policy": "model_edit_or_original_input_regeneration; one_extra_call_total; no_local_corrections",
                         "nli_promotion_enabled": False},
            "validation_models": {
                "embedding": {"name": settings.embedding_model, "device": settings.embedding_device},
                "cross_encoder": {"name": settings.cross_encoder_model, "device": settings.cross_encoder_device},

            },
            "pdf_count": len(_pdf_map())}


# Preserve the public Python facade used by the read-only exporter and integrations.
# Route groups receive this application's state, so job locks and cancellation are shared.
from backend.api.routes.documents import create_router as create_documents_router
_documents_router, _documents_views = create_documents_router(sys.modules[__name__])
app.include_router(_documents_router)
list_pdfs = _documents_views.list_pdfs
view_pdf = _documents_views.view_pdf
upload_pdf = _documents_views.upload_pdf

from backend.api.routes.runs import create_router as create_runs_router
_runs_router, _runs_views = create_runs_router(sys.modules[__name__])
app.include_router(_runs_router)
start_run = _runs_views.start_run
resume_run = _runs_views.resume_run
redo_external_validation = _runs_views.redo_external_validation
recovery_options = _runs_views.recovery_options
_saved_extraction_options = _runs_views._saved_extraction_options
_start_document_run = _runs_views._start_document_run
review_source = _runs_views.review_source
start_claim_action = _runs_views.start_claim_action
stop_job = _runs_views.stop_job
job_status = _runs_views.job_status
start_pathway_assembly = _runs_views.start_pathway_assembly

from backend.api.routes.results import create_router as create_results_router
_results_router, _results_views = create_results_router(sys.modules[__name__])
app.include_router(_results_router)
pdf_logs = _results_views.pdf_logs
pdf_claims = _results_views.pdf_claims
pdf_graph = _results_views.pdf_graph
pdf_audit_graph = _results_views.pdf_audit_graph
pdf_source_graph = _results_views.pdf_source_graph
download_graphml = _results_views.download_graphml
master_graph = _results_views.master_graph
_current_pathway_graph = _results_views._current_pathway_graph
master_audit_graph = _results_views.master_audit_graph
master_source_graph = _results_views.master_source_graph
pdf_paths = _results_views.pdf_paths
global_pathways = _results_views.global_pathways
pdf_pathway_assessments = _results_views.pdf_pathway_assessments
pathway_assessments = _results_views.pathway_assessments
pdf_trust = _results_views.pdf_trust

dist = FRONTEND_DIST_DIR
if dist.exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
