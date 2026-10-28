"""Lean FastAPI viewer. Never imports the pipeline, providers, or live API."""
from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from backend.viewer.snapshot import Snapshot
from backend.stages.pathway_assembly.paths import assembled_paths


def create_app(snapshot_dir=None, frontend_dist=None):
    root = Path(__file__).resolve().parents[2]
    snapshot = Snapshot(snapshot_dir or os.getenv('VIEWER_DATA_DIR', str(root / 'deploy/render/snapshot')))
    dist = Path(frontend_dist or os.getenv('VIEWER_FRONTEND_DIR', str(root / 'frontend/dist')))

    @asynccontextmanager
    async def lifespan(app):
        snapshot.load()  # Reject a partial/mismatched publication before serving it.
        yield

    app = FastAPI(title='Research Results Viewer', lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.snapshot = snapshot
    app.add_middleware(GZipMiddleware, minimum_size=1000)

    @app.middleware('http')
    async def read_only(request, call_next):
        if request.method not in {'GET', 'HEAD', 'OPTIONS'}:
            return JSONResponse({'detail': 'This published results viewer is read-only.'}, status_code=403)
        response = await call_next(request)
        response.headers['X-Content-Type-Options'] = 'nosniff'
        return response

    @app.exception_handler(ValueError)
    async def integrity_error(request, exc):
        return JSONResponse({'detail': 'Published snapshot failed an integrity check.'}, status_code=503)

    def document(identifier):
        entry = snapshot.document(identifier)
        if not entry:
            raise HTTPException(404, 'Published document not found')
        return entry

    @app.get('/api/health')
    def health():
        m = snapshot.manifest
        return {'status': 'ok', 'mode': 'viewer',
                'capabilities': {'read_only': True, 'execute': False, 'upload': False},
                'snapshot': {'id': m['snapshot_id'], 'created_at': m['created_at'],
                             'documents': len(m['documents']), 'warnings': m.get('warnings', [])},
                'pipeline': m['pipeline']}

    @app.get('/api/pdfs')
    def pdfs():
        return snapshot.read(snapshot.manifest['catalog'])

    @app.get('/api/pdfs/{identifier}/file')
    def pdf_file(identifier: str):
        entry = document(identifier)
        if not entry.get('pdf'):
            raise HTTPException(404, 'The source PDF was not included in this publication')
        return Response(snapshot.bytes(entry['pdf']), media_type='application/pdf',
                        headers={'Content-Disposition': 'inline'})

    @app.get('/api/pdfs/{identifier}/graph.graphml')
    def graphml(identifier: str):
        entry = document(identifier)
        if not entry.get('graphml'):
            raise HTTPException(404, 'No saved GraphML export in this snapshot')
        return Response(snapshot.bytes(entry['graphml']), media_type='application/graphml+xml',
                        headers={'Content-Disposition': 'attachment; filename="knowledge_graph.graphml"'})

    @app.get('/api/pdfs/{identifier}/jobs')
    def jobs(identifier: str):
        entry = document(identifier)
        return [{k: row.get(k) for k in ('id', 'pdf_id', 'status', 'recorded_status', 'action', 'started_at', 'finished_at', 'error')}
                for row in sorted((snapshot.read(p) for p in entry['jobs'].values()),
                                  key=lambda row: row.get('started_at') or '', reverse=True)]

    @app.get('/api/jobs/{job_id}')
    def job(job_id: str):
        relative = snapshot.manifest['jobs'].get(job_id)
        if not relative:
            raise HTTPException(404, 'Saved job not included in this snapshot')
        return snapshot.read(relative)

    @app.get('/api/pdfs/{identifier}/recovery')
    def recovery(identifier: str):
        document(identifier)
        return {'can_continue': False, 'reason': 'Published results are read-only.'}

    @app.get('/api/pdfs/{identifier}/{kind}')
    def result(identifier: str, kind: str):
        if kind not in {'claims', 'logs', 'graph', 'audit-graph', 'source-graph', 'paths', 'trust'}:
            raise HTTPException(404, 'Result view not found')
        return snapshot.read(document(identifier)[kind])

    @app.get('/api/graph')
    @app.get('/api/audit-graph')
    @app.get('/api/source-graph')
    def global_graph(request: Request):
        return snapshot.read(snapshot.manifest['graphs'][request.url.path.rsplit('/', 1)[-1]])

    @app.get('/api/pathways')
    def pathways(limit: int = Query(100, ge=1, le=500), max_hops: int = Query(6, ge=2, le=12),
                 offset: int = Query(0, ge=0), view: Literal['validated', 'provisional'] = 'validated'):
        data = snapshot.read(snapshot.manifest['pathways'][view])
        return {**data, **assembled_paths(data['graph'], limit, max_hops, offset)}

    @app.get('/api/{unknown:path}')
    def unknown_api(unknown: str):
        raise HTTPException(404, 'Viewer endpoint not found')

    if dist.is_dir():
        app.mount('/', StaticFiles(directory=dist, html=True), name='frontend')
    return app


app = create_app()
