"""Documents routes, with application state injected for shared jobs and saved views."""

from __future__ import annotations
from fastapi import APIRouter
import re
from pathlib import Path
from typing import Any
from fastapi import File, HTTPException, UploadFile
from fastapi.responses import FileResponse
from backend.shared.storage import stable_hash
from backend.shared.jobs import ACTIVE_STATUSES
from backend.config.pipeline import VALIDATION_STAGES_PAUSED


class DocumentsRoutes:
    def __init__(self, workspace):
        self.workspace = workspace

    def list_pdfs(self) -> list[dict[str, Any]]:
        items = []
        with self.workspace._lock:
            for pdf_id, pdf in self.workspace._pdf_map().items():
                output = self.workspace._output_dir(pdf)
                summary = self.workspace._summary_with_coverage(output)
                # A PDF-quality failure happens before a summary can be written.
                # Keep the last scope/text-quality finding visible in the workbench
                # even for that early, intentional stop.
                document_scope = self.workspace._read_json(output / "document_scope.json")
                active = next((job for job in self.workspace._jobs.values() if job["pdf_id"] == pdf_id and job["status"] in ACTIVE_STATUSES), None)
                saved_status = summary.get("run_status", "completed") if isinstance(summary, dict) else "not_started"
                state = self.workspace._read_json(output / 'run_state.json', {})
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
                               "can_resume": (saved_status in {'failed', 'stopped'} or bool((summary or {}).get('retrieval_pending'))
                                              or bool(((summary or {}).get('pathway_validation') or {}).get('coverage',{}).get('has_more'))) and self.workspace.resume_available(output),
                               "can_redo_external_validation": not VALIDATION_STAGES_PAUSED and self.workspace.resume_available(output),
                               "latest_job_id": state.get('id'),
                               "active_job_id": active["id"] if active else None})
        return items

    def view_pdf(self, pdf_id: str) -> FileResponse:
        return FileResponse(self.workspace._pdf_path(pdf_id), media_type="application/pdf", filename=self.workspace._pdf_path(pdf_id).name,
                            content_disposition_type="inline")

    async def upload_pdf(self, file: UploadFile = File(...)) -> dict[str, Any]:
        name = Path((file.filename or "").replace("\\", "/")).name
        name = re.sub(r"[^A-Za-z0-9 ._()-]", "_", name).strip()
        if not name.lower().endswith(".pdf") or not name or name.startswith("."):
            raise HTTPException(status_code=400, detail="Choose a PDF file")
        target = self.workspace.PDF_DIR / name
        if target.exists():
            raise HTTPException(status_code=409, detail="A PDF with this name already exists")
        contents = await file.read(50 * 1024 * 1024 + 1)
        if len(contents) > 50 * 1024 * 1024:
            raise HTTPException(status_code=413, detail="PDF exceeds the 50 MB upload limit")
        if not contents.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="File content is not a PDF")
        self.workspace.PDF_DIR.mkdir(parents=True, exist_ok=True)
        target.write_bytes(contents)
        return {"id": stable_hash(name, 16), "name": name}


def create_router(workspace):
    views = DocumentsRoutes(workspace)
    router = APIRouter()
    router.add_api_route('/api/pdfs', views.list_pdfs, methods=['GET'])
    router.add_api_route('/api/pdfs/{pdf_id}/file', views.view_pdf, methods=['GET'])
    router.add_api_route('/api/pdfs', views.upload_pdf, methods=['POST'])
    return router, views
