"""FastAPI server for the local crop to human health workbench."""

from __future__ import annotations

import json
import re
import threading
import uuid
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, File, HTTPException, UploadFile, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, StrictBool, ConfigDict

from backend.config.settings import ONTOLOGY_DIR, PDF_DIR, SCHEMA_VERSION, Settings
from backend.config.paths import FRONTEND_DIST_DIR
from backend.providers.llm import OpenCodeClient, model_reasoning_policy
from backend.stages.external_validation.joint import verified_contradictions
from backend.shared.storage import read_jsonl, stable_hash
from backend.shared.artifacts import ARTIFACT_WRITE_POLICY

from backend.stages.graph_construction.graph import merge_graphs
from backend.pipelines.document import run_pipeline
from backend.pipelines.checkpoints import resume_available, resume_metadata, read_metadata
from backend.shared.activity import display_error, display_log, display_request, progress_message, activity_stage, research_activity
from backend.pipelines.claim_repair import repairable, run_claim_action, refresh_saved_views
from backend.shared.jobs import ACTIVE_STATUSES, save_job, read_job, interrupt_job
from backend.shared.cancellation import CancellationToken, PipelineStopped, cancellation_scope, check_cancelled
from backend.pipelines.pathways import assemble_saved_pathways
from backend.stages.pathway_assembly.assembly import read_assembly
from backend.stages.pathway_assembly.corpus import ASSEMBLY_ELIGIBILITY_POLICY
from backend.stages.source_validation.abbreviations import SOURCE_VALIDATION_POLICY
from backend.stages.pathway_assembly.paths import assembled_paths
from backend.stages.trust_graph.ledger import evidence_balance, decorate_graph, connection_assessments


app = FastAPI(title="Crop to Human Health Workbench", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["*"])


class ThinkingOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    extraction: StrictBool = False
    source: StrictBool = False
    query: StrictBool = False
    external: StrictBool = False
    pathway_assembly: StrictBool = False


class RunOptions(BaseModel):
    knowledge_building_mode: Literal['trust_graph', 'validated_first'] = Field(default_factory=lambda: Settings().knowledge_building_mode)
    trust_batch_size: int = Field(default_factory=lambda: Settings().trust_batch_size, ge=1, le=6)
    thinking: ThinkingOptions = Field(default_factory=ThinkingOptions)
    max_pages: int = Field(default=0, ge=0, le=1000, description="0 means all text pages")
    max_claims: int = Field(default=0, ge=0, le=500, description="0 means all extracted claims")
    articles_per_claim: int = Field(default_factory=lambda: Settings().external_top_k, ge=1, le=30,
                                    description="Top-k search papers per claim; all usable attached citations are additional")
    claims_per_packet: int = Field(default_factory=lambda: Settings().extraction_claims_per_packet, ge=0, le=8)
    extraction_mode: Literal['target_context', 'whole_packet'] = Field(
        default_factory=lambda: Settings().extraction_mode)
    extraction_target_words: int = Field(default_factory=lambda: Settings().extraction_target_words, ge=200, le=2000)
    extraction_generation_budget: int = Field(
        default_factory=lambda: Settings().extraction_min_output_budget, ge=8192, le=32768,
        description='Initial generation allowance shared by reasoning and the final answer')


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
        _emit(job_id, "stage", label)
        if _jobs[job_id].get('action') == 'assemble_pathways':
            result = assemble_saved_pathways(ONTOLOGY_DIR, _output_dir(pdf), lambda level,message:_emit(job_id, level, message), thinking=options.thinking.model_dump())
        elif _jobs[job_id].get('action'):
            result = run_claim_action(pdf, _output_dir(pdf), _jobs[job_id]['candidate_id'],
                                      _jobs[job_id]['action'], options.articles_per_claim,
                                      lambda level, message: _emit(job_id, level, message), thinking=options.thinking.model_dump(),
                                      knowledge_building_mode=options.knowledge_building_mode, trust_batch_size=options.trust_batch_size)
        else:
            result = run_pipeline(pdf, options.max_pages, options.max_claims, options.articles_per_claim,
                                  lambda level, message: _emit(job_id, level, message),
                                  claims_per_packet=options.claims_per_packet,
                                  extraction_mode=options.extraction_mode,
                                  extraction_target_words=options.extraction_target_words,
                                  extraction_generation_budget=options.extraction_generation_budget,
                                  thinking=options.thinking.model_dump(),
                                  knowledge_building_mode=options.knowledge_building_mode, trust_batch_size=options.trust_batch_size,
                                  resume=bool(_jobs[job_id].get('resume')))
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
    if settings.external_judgement_mode in {'per_paper_synthesis', 'trust_graph'} or settings.knowledge_building_mode == 'trust_graph':
        ready['external'] = all(phase_ready.values())
    extraction_ready, validation_ready, helper_ready = (ready[task] for task in ('extraction', 'external', 'query'))
    return {"status": "ok", "model": settings.external_model, "model_ready": all(ready.values()),
            "available_models": opencode_models,
            "extraction": {"provider": "opencode", "model": settings.extraction_model,
                           "ready": extraction_ready, "available_models": opencode_models},
            "validation": {"provider": "opencode", "model": settings.external_model,
                           "ready": validation_ready, "available_models": opencode_models},
            "helper": {"provider": "opencode", "model": settings.query_model, "ready": helper_ready},
            "pipeline": {"schema_version": SCHEMA_VERSION,
                         "artifact_write_policy": ARTIFACT_WRITE_POLICY,
                         "pathway_assembly_policy": ASSEMBLY_ELIGIBILITY_POLICY,
                         "source_validation_policy": SOURCE_VALIDATION_POLICY,
                         "external_judgement_mode": 'trust_graph' if settings.knowledge_building_mode == 'trust_graph' else settings.external_judgement_mode,
                         "knowledge_building_mode": settings.knowledge_building_mode,
                         "trust_batch_size": settings.trust_batch_size,
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
                         "query_batch_size": settings.query_batch_size,
                         "retrieval_workers": settings.retrieval_workers,
                         "external_paper_workers": settings.external_paper_workers,
                         "paper_ready_judgement": settings.paper_ready_judgement,
                         "evidence_index_enabled": settings.evidence_index_enabled,
                         "evidence_index_ttl_hours": settings.evidence_index_ttl_hours,
                         "search_wait_seconds": settings.search_wait_seconds,
                         "external_prefetch": settings.external_prefetch,
                         "embedding_batch_size": settings.embedding_batch_size,
                         "cross_encoder_batch_size": settings.cross_encoder_batch_size,
                         "opencode_transport": 'api',
                         "response_format_policy": settings.opencode_response_format,
                         "json_repair_attempts": settings.json_repair_attempts,
                         "json_regeneration_attempts": settings.json_regeneration_attempts,
                         "recovery_policy": "model_edit_or_original_input_regeneration; one_extra_call_total; no_local_corrections",
                         "nli_promotion_enabled": settings.enable_nli_promotion},
            "validation_models": {
                "embedding": {"name": settings.embedding_model, "device": settings.embedding_device},
                "cross_encoder": {"name": settings.cross_encoder_model, "device": settings.cross_encoder_device},
                "nli": {"name": settings.nli_model, "device": settings.nli_device,
                        "enabled": settings.enable_nli},
            },
            "pdf_count": len(_pdf_map())}


@app.get("/api/pdfs")
def list_pdfs() -> list[dict[str, Any]]:
    items = []
    with _lock:
        for pdf_id, pdf in _pdf_map().items():
            output = _output_dir(pdf)
            summary = _summary_with_coverage(output)
            # A PDF-quality failure happens before a summary can be written.
            # Keep the last scope/text-quality finding visible in the workbench
            # even for that early, intentional stop.
            document_scope = _read_json(output / "document_scope.json")
            active = next((job for job in _jobs.values() if job["pdf_id"] == pdf_id and job["status"] in ACTIVE_STATUSES), None)
            saved_status = summary.get("run_status", "completed") if isinstance(summary, dict) else "not_started"
            state = _read_json(output / 'run_state.json', {})
            if state.get('status') in {'failed', 'stopped'}:
                saved_status = state['status']
            if state.get('status') in ACTIVE_STATUSES and not active:
                saved_status = 'failed'  # Interrupted process; saved work can be resumed.
            if saved_status == 'completed' and (summary.get('extraction_coverage') or {}).get('complete') is False:
                saved_status = 'completed_with_warnings'
            items.append({"id": pdf_id, "name": pdf.name, "title": pdf.stem,
                          "size_bytes": pdf.stat().st_size,
                           "status": active["status"] if active else saved_status,
                           "summary": summary, "document_scope": document_scope,
                           "can_resume": (saved_status in {'failed', 'stopped'} or bool((summary or {}).get('retrieval_pending'))) and resume_available(output),
                           "latest_job_id": state.get('id'),
                           "active_job_id": active["id"] if active else None})
    return items


@app.get("/api/pdfs/{pdf_id}/file")
def view_pdf(pdf_id: str) -> FileResponse:
    return FileResponse(_pdf_path(pdf_id), media_type="application/pdf", filename=_pdf_path(pdf_id).name,
                        content_disposition_type="inline")


@app.post("/api/pdfs")
async def upload_pdf(file: UploadFile = File(...)) -> dict[str, Any]:
    name = Path((file.filename or "").replace("\\", "/")).name
    name = re.sub(r"[^A-Za-z0-9 ._()-]", "_", name).strip()
    if not name.lower().endswith(".pdf") or not name or name.startswith("."):
        raise HTTPException(status_code=400, detail="Choose a PDF file")
    target = PDF_DIR / name
    if target.exists():
        raise HTTPException(status_code=409, detail="A PDF with this name already exists")
    contents = await file.read(50 * 1024 * 1024 + 1)
    if len(contents) > 50 * 1024 * 1024:
        raise HTTPException(status_code=413, detail="PDF exceeds the 50 MB upload limit")
    if not contents.startswith(b"%PDF-"):
        raise HTTPException(status_code=400, detail="File content is not a PDF")
    PDF_DIR.mkdir(parents=True, exist_ok=True)
    target.write_bytes(contents)
    return {"id": stable_hash(name, 16), "name": name}


@app.post("/api/pdfs/{pdf_id}/run", status_code=202)
def start_run(pdf_id: str, options: RunOptions) -> dict[str, Any]:
    return _start_document_run(pdf_id, options)


@app.post('/api/pdfs/{pdf_id}/resume', status_code=202)
def resume_run(pdf_id: str, options: RunOptions) -> dict[str, Any]:
    return _start_document_run(pdf_id, options, resume=True)


@app.get('/api/pdfs/{pdf_id}/recovery')
def recovery_options(pdf_id: str) -> dict[str, Any]:
    """Check saved progress for a user choice; never queue work from this GET."""
    pdf = _pdf_path(pdf_id)
    output = _output_dir(pdf)
    state = _read_json(output / 'run_state.json', {})
    state = state if isinstance(state, dict) else {}
    saved = state.get('options') or {}
    saved = saved if isinstance(saved, dict) else {}
    saved.setdefault('knowledge_building_mode', 'validated_first')
    options = {key: value for key, value in saved.items() if key in RunOptions.model_fields}
    reason, can_continue = None, False
    try:
        checkpoint = read_metadata(output) or {}
        options.update(_saved_extraction_options(output))
        for key in ('max_pages', 'claims_per_packet'):
            if checkpoint.get(key) is not None:
                options[key] = checkpoint[key]
        # Historical runs may predate saved options and checkpoint metadata.
        if not checkpoint and 'max_pages' not in options:
            quality = _read_json(output / 'pdf_quality.json', {})
            selected, total = quality.get('selected_pages'), quality.get('total_pages')
            if isinstance(selected, int) and isinstance(total, int) and selected < total:
                options['max_pages'] = selected
        restored_options = RunOptions(**options)
        options = restored_options.model_dump()
        with _lock:
            active = any(job.get('pdf_id') == pdf_id and job.get('status') in ACTIVE_STATUSES
                         for job in _jobs.values())
        if active:
            reason = 'This paper already has an active run.'
        elif state.get('status') in {'completed', 'completed_with_warnings'}:
            reason = 'This run has already finished. Its saved results remain available.'
        else:
            resume_metadata(output, pdf, replace(Settings(), thinking=restored_options.thinking.model_dump()), restored_options.max_pages,
                            restored_options.claims_per_packet, extraction_mode=restored_options.extraction_mode,
                            target_words=restored_options.extraction_target_words)
            can_continue = True
    except (ValueError, OSError) as exc:
        reason = str(exc)
    return {'can_continue': can_continue, 'reason': reason, 'options': options,
            'job_id': state.get('id'), 'error': display_error(state.get('error')),
            'job_missing': not state.get('id') or (
                state['id'] not in _jobs and read_job(output, state['id']) is None),
            'interrupted': bool(state.get('interrupted')) or state.get('status') in ACTIVE_STATUSES}


def _saved_extraction_options(output: Path) -> dict[str, Any]:
    metadata = read_metadata(output) or _read_json(output / 'summary.json', {}) or {}
    defaults = Settings()
    # Runs predating thinking metadata used On. Preserve their extraction mode
    # on recovery even though fresh runs now default to Off.
    saved_thinking = metadata.get('thinking') or ({task: True for task in defaults.task_models} if metadata else defaults.task_thinking)
    restored = {'thinking': saved_thinking, 'extraction_mode': metadata.get('extraction_mode', 'whole_packet'),
            'knowledge_building_mode': metadata.get('knowledge_building_mode', 'validated_first'),
            'trust_batch_size': metadata.get('trust_batch_size', defaults.trust_batch_size),
            'extraction_target_words': metadata.get('extraction_target_words') or defaults.extraction_target_words,
            'extraction_generation_budget': metadata.get('extraction_generation_budget') or defaults.extraction_min_output_budget}
    for key in ('max_pages', 'claims_per_packet'):
        if metadata.get(key) is not None:
            restored[key] = metadata[key]
    return restored


def _start_document_run(pdf_id: str, options: RunOptions, resume: bool = False) -> dict[str, Any]:
    pdf = _pdf_path(pdf_id)
    if resume:
        try:
            # An omitted new option on a historical resume must not switch its
            # extraction policy to the new default or reinterpret saved work.
            restored = {key: value for key, value in _saved_extraction_options(_output_dir(pdf)).items()
                        if key not in options.model_fields_set}
            options = RunOptions(**{**options.model_dump(), **restored})
            resume_metadata(_output_dir(pdf), pdf, replace(Settings(), thinking=options.thinking.model_dump()), options.max_pages, options.claims_per_packet,
                            extraction_mode=options.extraction_mode, target_words=options.extraction_target_words)
        except (ValueError, OSError) as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
    try:
        settings = Settings()
        available_models = set(OpenCodeClient(settings).health_check().get("models", []))
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"OpenCode is unavailable: {exc}") from exc
    missing = [f'{task}: {model}' for task, model in settings.task_models.items()
               if model.split('#', 1)[0] not in available_models]
    if settings.external_judgement_mode == 'per_paper_synthesis' and settings.external_paper_model not in available_models:
        missing.append(f'per-paper external: {settings.external_paper_model}')
    if missing:
        raise HTTPException(status_code=400, detail=f"Required OpenCode model is unavailable: {', '.join(missing)}")
    with _lock:
        if any(job["status"] in ACTIVE_STATUSES for job in _jobs.values()):
            raise HTTPException(status_code=409, detail="A pipeline is already running. Wait for it to finish.")
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "pdf_id": pdf_id, "pdf_path": str(pdf), "status": "queued", "resume": resume,
               "logs": read_jsonl(_output_dir(pdf) / 'run_log.jsonl') if resume else [],
               "options": options.model_dump(), "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "updated_at": None, "finished_at": None, "result": None, "error": None}
        _jobs[job_id] = job
        output_dir = _output_dir(pdf)
        output_dir.mkdir(parents=True, exist_ok=True)
        filenames = () if resume else ("summary.json", "document_scope.json", "graph.json", "source_graph.json", "audit_graph.json", "knowledge_graph.graphml", "reasoning_paths.json", "candidates.jsonl",
                         "extraction_rejections.jsonl", "schema_valid_candidates.jsonl", "semantic_validation.jsonl", "semantic_valid_candidates.jsonl",
                         "source_validation.jsonl", "source_entailed_candidates.jsonl",
                         "validation_runs.jsonl", "evaluation_sample.jsonl", "raw_triplets.txt", "llm_requests.jsonl", "extraction_packets.jsonl", "run_state.json", "claim_revisions.jsonl",
                         "pipeline_checkpoint.json", "query_translations.jsonl", "extraction_contexts.jsonl")
        for filename in filenames:
            (output_dir / filename).unlink(missing_ok=True)
        log_path = output_dir / "run_log.jsonl"
        if not resume:
            log_path.write_text("", encoding="utf-8")
        _persist_job(job_id)
    threading.Thread(target=_worker, args=(job_id, options), daemon=True, name=f"pdf-run-{pdf_id}").start()
    return _job_snapshot(job)


@app.post('/api/pdfs/{pdf_id}/claims/{candidate_id}/{action}', status_code=202)
def start_claim_action(pdf_id: str, candidate_id: str, action: str, options: RunOptions) -> dict[str, Any]:
    """Queue a deliberate, single-claim operation without clearing PDF artifacts."""
    if action not in {'repair', 'retry_external'}:
        raise HTTPException(status_code=404, detail='Unknown claim action')
    pdf = _pdf_path(pdf_id)
    output = _output_dir(pdf)
    with _lock:
        if any(job['status'] in ACTIVE_STATUSES for job in _jobs.values()):
            raise HTTPException(status_code=409, detail='Wait for the current pipeline or claim action to finish')
        candidate = next((c for c in read_jsonl(output / 'candidates.jsonl') if c['candidate_id'] == candidate_id), None)
        if candidate is None:
            raise HTTPException(status_code=404, detail='Claim not found')
        validation = next((r for r in read_jsonl(output / 'validation_runs.jsonl') if r['candidate_id'] == candidate_id), None)
        if action == 'repair' and not repairable(candidate, validation):
            raise HTTPException(status_code=400, detail='Repair requires source review/correction or a verified external contradiction')
        if action == 'retry_external' and (candidate.get('schema_error') or
                candidate.get('source_validation', {}).get('decision') != 'entailed'):
            raise HTTPException(status_code=400, detail='This claim must pass schema and source fidelity checks first')
        job_id = uuid.uuid4().hex
        job = {'id': job_id, 'pdf_id': pdf_id, 'pdf_path': str(pdf), 'candidate_id': candidate_id,
               'action': action, 'status': 'queued', 'logs': read_jsonl(output / 'run_log.jsonl'),
               'options': options.model_dump(), 'started_at': datetime.now(timezone.utc).isoformat(timespec='seconds'),
               'updated_at': None, 'finished_at': None, 'result': None, 'error': None}
        _jobs[job_id] = job
        _persist_job(job_id)
    threading.Thread(target=_worker, args=(job_id, options), daemon=True, name=f'claim-{candidate_id}').start()
    return _job_snapshot(job)


@app.post('/api/jobs/{job_id}/stop', status_code=202)
def stop_job(job_id: str):
    with _lock:
        job = _jobs.get(job_id) or _saved_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail='Job not found')
        if job.get('status') not in ACTIVE_STATUSES:
            return _job_snapshot(job)
        if not job.get('stop_requested'):
            job.update(status='stopping', stop_requested=True)
            _cancellations.setdefault(job_id, CancellationToken()).request()
            _emit(job_id, 'info', 'Stop requested. Cancelling local model waits and waiting for active local operations to release. Saved checkpoints are preserved.')
        return _job_snapshot(job)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    with _lock:
        job = _jobs.get(job_id) or _saved_job(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return _job_snapshot(job)


@app.get("/api/pdfs/{pdf_id}/logs")
def pdf_logs(pdf_id: str) -> list[dict[str, Any]]:
    return [display_log(entry) for entry in read_jsonl(_output_dir(_pdf_path(pdf_id)) / 'run_log.jsonl') if research_activity(entry)]


@app.get("/api/pdfs/{pdf_id}/claims")
def pdf_claims(pdf_id: str) -> dict[str, Any]:
    output = _output_dir(_pdf_path(pdf_id))
    candidates = read_jsonl(output / "candidates.jsonl")
    results = read_jsonl(output / "validation_runs.jsonl")
    by_id = {item["candidate_id"]: item for item in results}
    revisions = read_jsonl(output / 'claim_revisions.jsonl')
    claims = []
    for candidate in candidates:
        validation = by_id.get(candidate['candidate_id'])
        conflicts = verified_contradictions(candidate, validation)
        claims.append({'candidate': candidate, 'validation': validation, 'trust': evidence_balance(candidate, validation),
                       'repair_eligibility': {'allowed': bool(conflicts) or repairable(candidate),
                                              'external_contradictions': conflicts},
                       'revisions': [row for row in revisions if row['candidate_id'] == candidate['candidate_id']]})
    return {"claims": claims, "total": len(candidates),
            "extraction_rejections": read_jsonl(output / "extraction_rejections.jsonl"),
            "model_requests": [display_request(row) for row in read_jsonl(output / 'llm_requests.jsonl')]}


@app.get("/api/pdfs/{pdf_id}/graph")
def pdf_graph(pdf_id: str) -> dict[str, Any]:
    return _read_json(_output_dir(_pdf_path(pdf_id)) / "graph.json",
                      {"nodes": [], "edges": [], "counts": {"nodes": 0, "edges": 0, "corroborated": 0}})


@app.get("/api/pdfs/{pdf_id}/audit-graph")
def pdf_audit_graph(pdf_id: str) -> dict[str, Any]:
    return _read_json(_output_dir(_pdf_path(pdf_id)) / "audit_graph.json",
                      {"nodes": [], "edges": [], "counts": {"nodes": 0, "edges": 0, "corroborated": 0}})


@app.get("/api/pdfs/{pdf_id}/source-graph")
def pdf_source_graph(pdf_id: str) -> dict[str, Any]:
    return _read_json(_output_dir(_pdf_path(pdf_id)) / "source_graph.json",
                      {"nodes": [], "edges": [], "counts": {"nodes": 0, "edges": 0, "source_supported": 0}})


@app.get("/api/pdfs/{pdf_id}/graph.graphml")
def download_graphml(pdf_id: str) -> FileResponse:
    path = _output_dir(_pdf_path(pdf_id)) / "knowledge_graph.graphml"
    if not path.exists():
        raise HTTPException(status_code=404, detail="Run the pipeline before downloading GraphML")
    return FileResponse(path, media_type="application/graphml+xml", filename=path.name)


@app.get("/api/graph")
def master_graph() -> dict[str, Any]:
    graphs = [_read_json(path / "graph.json", {"nodes": [], "edges": []}) for path in ONTOLOGY_DIR.iterdir() if path.is_dir()]
    return merge_graphs(graphs)


@app.get("/api/audit-graph")
def master_audit_graph() -> dict[str, Any]:
    graphs = [_read_json(path / "audit_graph.json", {"nodes": [], "edges": []}) for path in ONTOLOGY_DIR.iterdir() if path.is_dir()]
    return merge_graphs(graphs)


@app.get("/api/source-graph")
def master_source_graph() -> dict[str, Any]:
    graphs = [_read_json(path / "source_graph.json", {"nodes": [], "edges": []}) for path in ONTOLOGY_DIR.iterdir() if path.is_dir()]
    return merge_graphs(graphs)


@app.get("/api/pdfs/{pdf_id}/paths")
def pdf_paths(pdf_id: str) -> list[Any]:
    return _read_json(_output_dir(_pdf_path(pdf_id)) / "reasoning_paths.json", [])


@app.get('/api/pathways')
def global_pathways(limit: int = Query(default=100, ge=1, le=500),
                    max_hops: int = Query(default=6, ge=2, le=12), offset: int = Query(default=0, ge=0),
                    view: Literal['validated', 'provisional'] = 'validated'):
    data = read_assembly(ONTOLOGY_DIR, Settings().pathway_assembly_model, provisional=True) if view == 'provisional' else read_assembly(ONTOLOGY_DIR, Settings().pathway_assembly_model)
    snapshot = data.pop('snapshot') or {}
    graph = snapshot.get('graph', {'nodes':[], 'edges':[], 'counts':{}})
    if view == 'provisional':
        graph = decorate_graph(ONTOLOGY_DIR, graph)
    return {**data, 'graph':graph, 'counts':snapshot.get('counts', {}),
            'view': view, 'score_is_probability': False,
            'pathway_connections': connection_assessments(ONTOLOGY_DIR) if view == 'provisional' else [],
            'completed_at':snapshot.get('completed_at'), **assembled_paths(graph, limit, max_hops, offset)}


@app.get('/api/pdfs/{pdf_id}/trust')
def pdf_trust(pdf_id: str):
    output = _output_dir(_pdf_path(pdf_id))
    result = {r['candidate_id']: r for r in read_jsonl(output / 'validation_runs.jsonl')}
    return {'claims': [evidence_balance(c, result.get(c['candidate_id'])) for c in read_jsonl(output / 'candidates.jsonl')],
            'connections': [r for r in connection_assessments(ONTOLOGY_DIR) if r['document_key'] == output.name],
            'summary': _read_json(output / 'trust_summary.json', {}), 'score_is_probability': False}


@app.post('/api/pdfs/{pdf_id}/assemble-pathways', status_code=202)
def start_pathway_assembly(pdf_id: str, options: RunOptions | None = None):
    pdf = _pdf_path(pdf_id)
    with _lock:
        if any(job['status'] in ACTIVE_STATUSES for job in _jobs.values()):
            raise HTTPException(status_code=409, detail='Wait for the current pipeline to finish')
        job_id = uuid.uuid4().hex
        options = options or RunOptions()
        job = dict(id=job_id, pdf_id=pdf_id, pdf_path=str(pdf), status='queued', action='assemble_pathways',
                   logs=read_jsonl(_output_dir(pdf) / 'run_log.jsonl'), options=options.model_dump(),
                   started_at=datetime.now(timezone.utc).isoformat(timespec='seconds'),
                   updated_at=None, finished_at=None, result=None, error=None)
        _jobs[job_id] = job
        _output_dir(pdf).mkdir(parents=True, exist_ok=True)
        _persist_job(job_id)
    threading.Thread(target=_worker, args=(job_id, options), daemon=True, name='global-pathway-assembly').start()
    return _job_snapshot(job)


dist = FRONTEND_DIST_DIR
if dist.exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
