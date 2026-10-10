"""FastAPI server for the local crop to human health workbench."""

from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from external_validator.config import BLOCKED_PIPELINE_MODELS, DEFAULT_MODEL, ONTOLOGY_DIR, PDF_DIR, PROJECT_ROOT, Settings
from external_validator.llm import OllamaClient
from external_validator.storage import read_jsonl, stable_hash

from .graph import merge_graphs
from .pipeline import run_pipeline


app = FastAPI(title="Crop to Human Health Workbench", version="1.0.0")
app.add_middleware(CORSMiddleware, allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
                   allow_credentials=False, allow_methods=["GET", "POST"], allow_headers=["*"])


class RunOptions(BaseModel):
    max_pages: int = Field(default=0, ge=0, le=1000, description="0 means all text pages")
    max_claims: int = Field(default=0, ge=0, le=500, description="0 means all extracted claims")
    articles_per_claim: int = Field(default=3, ge=1, le=8)
    claims_per_packet: int = Field(default=4, ge=1, le=8)
    model: str = Field(default=DEFAULT_MODEL, min_length=1, max_length=120)


_jobs: dict[str, dict[str, Any]] = {}
_lock = threading.RLock()


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
    return {key: value for key, value in job.items() if key != "pdf_path"}


def _emit(job_id: str, level: str, message: str) -> None:
    entry = {"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "level": level, "message": message}
    with _lock:
        job = _jobs[job_id]
        job["logs"].append(entry)
        job["updated_at"] = entry["at"]
        log_path = _output_dir(Path(job["pdf_path"])) / "run_log.jsonl"
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _worker(job_id: str, options: RunOptions) -> None:
    with _lock:
        _jobs[job_id]["status"] = "running"
        pdf = Path(_jobs[job_id]["pdf_path"])
    try:
        _emit(job_id, "stage", f"Starting pipeline for {pdf.name}")
        result = run_pipeline(pdf, options.max_pages, options.max_claims, options.articles_per_claim,
                              lambda level, message: _emit(job_id, level, message), model=options.model,
                              claims_per_packet=options.claims_per_packet)
        with _lock:
            _jobs[job_id]["status"] = "completed"
            _jobs[job_id]["result"] = result
    except Exception as exc:
        _emit(job_id, "error", str(exc))
        with _lock:
            _jobs[job_id]["status"] = "failed"
            _jobs[job_id]["error"] = str(exc)
    finally:
        with _lock:
            _jobs[job_id]["finished_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")


@app.get("/api/health")
def health() -> dict[str, Any]:
    settings = Settings()
    models: list[str] = []
    try:
        models = [item.get("name") for item in OllamaClient(settings).health_check().get("models", [])
                  if item.get("name") not in BLOCKED_PIPELINE_MODELS]
        model_ready = settings.model in models
    except Exception:
        model_ready = False
    return {"status": "ok", "model": settings.model, "model_ready": model_ready,
            "available_models": models,
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
            summary = _read_json(output / "summary.json")
            # A PDF-quality failure happens before a summary can be written.
            # Keep the last scope/text-quality finding visible in the workbench
            # even for that early, intentional stop.
            document_scope = _read_json(output / "document_scope.json")
            active = next((job for job in _jobs.values() if job["pdf_id"] == pdf_id and job["status"] in {"queued", "running"}), None)
            saved_status = summary.get("run_status", "completed") if isinstance(summary, dict) else "not_started"
            items.append({"id": pdf_id, "name": pdf.name, "title": pdf.stem,
                          "size_bytes": pdf.stat().st_size,
                           "status": "running" if active else saved_status,
                           "summary": summary, "document_scope": document_scope,
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
    pdf = _pdf_path(pdf_id)
    try:
        available_models = {item.get("name") for item in OllamaClient(Settings()).health_check().get("models", [])
                            if item.get("name") not in BLOCKED_PIPELINE_MODELS}
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"Ollama is unavailable: {exc}") from exc
    if options.model not in available_models:
        raise HTTPException(status_code=400, detail=f"Ollama model {options.model} is not installed")
    with _lock:
        if any(job["status"] in {"queued", "running"} for job in _jobs.values()):
            raise HTTPException(status_code=409, detail="A pipeline is already running. Wait for it to finish.")
        job_id = uuid.uuid4().hex
        job = {"id": job_id, "pdf_id": pdf_id, "pdf_path": str(pdf), "status": "queued", "logs": [],
               "options": options.model_dump(), "started_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
               "updated_at": None, "finished_at": None, "result": None, "error": None}
        _jobs[job_id] = job
        output_dir = _output_dir(pdf)
        output_dir.mkdir(parents=True, exist_ok=True)
        for filename in ("summary.json", "document_scope.json", "graph.json", "source_graph.json", "audit_graph.json", "knowledge_graph.graphml", "reasoning_paths.json", "candidates.jsonl",
                         "extraction_rejections.jsonl", "schema_valid_candidates.jsonl", "semantic_validation.jsonl", "semantic_valid_candidates.jsonl",
                         "source_validation.jsonl", "source_entailed_candidates.jsonl",
                         "validation_runs.jsonl", "evaluation_sample.jsonl", "raw_triplets.txt"):
            (output_dir / filename).unlink(missing_ok=True)
        log_path = output_dir / "run_log.jsonl"
        log_path.write_text("", encoding="utf-8")
    threading.Thread(target=_worker, args=(job_id, options), daemon=True, name=f"pdf-run-{pdf_id}").start()
    return _job_snapshot(job)


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str) -> dict[str, Any]:
    with _lock:
        job = _jobs.get(job_id)
        if job is None:
            raise HTTPException(status_code=404, detail="Job not found")
        return _job_snapshot(job)


@app.get("/api/pdfs/{pdf_id}/logs")
def pdf_logs(pdf_id: str) -> list[dict[str, Any]]:
    return read_jsonl(_output_dir(_pdf_path(pdf_id)) / "run_log.jsonl")


@app.get("/api/pdfs/{pdf_id}/claims")
def pdf_claims(pdf_id: str) -> dict[str, Any]:
    output = _output_dir(_pdf_path(pdf_id))
    candidates = read_jsonl(output / "candidates.jsonl")
    results = read_jsonl(output / "validation_runs.jsonl")
    by_id = {item["candidate_id"]: item for item in results}
    return {"claims": [{"candidate": candidate, "validation": by_id.get(candidate["candidate_id"])}
                       for candidate in candidates], "total": len(candidates)}


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


dist = PROJECT_ROOT / "frontend" / "dist"
if dist.exists():
    from fastapi.staticfiles import StaticFiles
    app.mount("/", StaticFiles(directory=dist, html=True), name="frontend")
