"""Lean FastAPI viewer. Never imports the pipeline, providers, or live API."""
from contextlib import asynccontextmanager
import os
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, Response, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.gzip import GZipMiddleware
from backend.viewer.snapshot import Snapshot, safe_path
from backend.viewer.db import PublicationDB
from backend.stages.pathway_assembly.paths import assembled_paths


def create_app(snapshot_dir=None, frontend_dist=None):
    root = Path(__file__).resolve().parents[2]
    snapshot_path = Path(snapshot_dir or os.getenv('VIEWER_DATA_DIR', str(root / 'deploy/render/snapshot'))).resolve()
    snapshot = Snapshot(snapshot_path)
    db = PublicationDB(snapshot_path / 'publication.db')
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
        if request.url.path.startswith('/api/'):
            response.headers['Cache-Control'] = 'public, max-age=86400, stale-while-revalidate=604800'
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
        m = snapshot.manifest or {}
        return {'status': 'ok', 'mode': 'viewer',
                'capabilities': {'read_only': True, 'execute': False, 'upload': False},
                'snapshot': {'id': m.get('snapshot_id'), 'created_at': m.get('created_at'),
                             'documents': len(m.get('documents', {})), 'warnings': m.get('warnings', [])},
                'pipeline': m.get('pipeline', {})}

    @app.get('/api/pdfs')
    def pdfs():
        data = snapshot.bytes(snapshot.manifest['catalog'])
        if db.is_available():
            catalog_raw = db.get_catalog_raw()
            if catalog_raw:
                return Response(catalog_raw, media_type='application/json')
        return Response(data, media_type='application/json')

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
        if db.is_available():
            doc = db.get_document(identifier)
            if not doc:
                raise HTTPException(404, 'Published document not found')
            return db.get_jobs(identifier)
        entry = document(identifier)
        return [{k: row.get(k) for k in ('id', 'pdf_id', 'status', 'recorded_status', 'action', 'started_at', 'finished_at', 'error')}
                for row in sorted((snapshot.read(p) for p in entry['jobs'].values()),
                                  key=lambda row: row.get('started_at') or '', reverse=True)]

    @app.get('/api/jobs/{job_id}')
    def job(job_id: str):
        if db.is_available():
            raw = db.get_job_raw(job_id)
            if raw:
                return Response(raw, media_type='application/json')
            raise HTTPException(404, 'Saved job not included in this snapshot')
        relative = snapshot.manifest['jobs'].get(job_id)
        if not relative:
            raise HTTPException(404, 'Saved job not included in this snapshot')
        return snapshot.read(relative)

    @app.get('/api/pdfs/{identifier}/recovery')
    def recovery(identifier: str):
        document(identifier)
        return {'can_continue': False, 'reason': 'Published results are read-only.'}

    @app.get('/api/pdfs/{identifier}/{kind}')
    def result(identifier: str, kind: str, limit: int = Query(0, ge=0), offset: int = Query(0, ge=0)):
        if kind not in {'claims', 'logs', 'graph', 'audit-graph', 'source-graph', 'paths', 'trust', 'pathway-assessments'}:
            raise HTTPException(404, 'Result view not found')
        if db.is_available():
            if kind == 'claims':
                claims_raw = db.get_claims_response_raw(identifier, limit=limit, offset=offset)
                if claims_raw is not None:
                    return Response(claims_raw, media_type='application/json')
            elif kind == 'pathway-assessments':
                pa_raw = db.get_document_pathway_assessments_raw(identifier)
                if pa_raw is not None:
                    return Response(pa_raw, media_type='application/json')
                return JSONResponse({'records': [], 'counts': {}, 'historical_snapshot': True})
            else:
                view_raw = db.get_document_view_raw(identifier, kind)
                if view_raw is not None:
                    return Response(view_raw, media_type='application/json')
        entry = document(identifier)
        if kind == 'pathway-assessments' and kind not in entry:
            return JSONResponse({'records': [], 'counts': {}, 'historical_snapshot': True})
        return Response(snapshot.bytes(entry[kind]), media_type='application/json')

    @app.get('/api/pathway-assessments')
    def assessments():
        if db.is_available():
            pa_raw = db.get_global_pathway_assessments_raw()
            if pa_raw:
                return Response(pa_raw, media_type='application/json')
        relative = snapshot.manifest.get('pathway_assessments')
        if not relative:
            return JSONResponse({'records': [], 'counts': {}, 'historical_snapshot': True})
        return Response(snapshot.bytes(relative), media_type='application/json')

    @app.get('/api/graph')
    @app.get('/api/audit-graph')
    @app.get('/api/source-graph')
    def global_graph(request: Request):
        kind = request.url.path.rsplit('/', 1)[-1]
        if db.is_available():
            raw = db.get_global_view_raw(kind)
            if raw:
                return Response(raw, media_type='application/json')
        relative = snapshot.manifest['graphs'].get(kind)
        if not relative:
            raise HTTPException(404, 'Graph not found')
        return Response(snapshot.bytes(relative), media_type='application/json')

    _validated_cache = None

    def get_validated_records():
        nonlocal _validated_cache
        if _validated_cache is None:
            relative = snapshot.manifest.get('pathway_assessments')
            if not relative:
                return []
            raw = snapshot.read(relative)
            _validated_cache = [
                {
                    'pathway_id': r['pathway_id'],
                    'eligible': r.get('eligible', False),
                    'historical_eligible': r.get('historical_eligible', False),
                    'stale': r.get('stale', False),
                    'target': {'edges': r['target']['edges']},
                    'verdict': r.get('verdict'),
                    'scientific_verdict': r.get('scientific_verdict'),
                    'paper_assessments': r.get('paper_assessments', []),
                }
                for r in raw.get('records', [])
            ]
        return _validated_cache

    @app.get('/api/pathways')
    def pathways(limit: int = Query(100, ge=1, le=500), max_hops: int = Query(6, ge=2, le=12),
                 offset: int = Query(0, ge=0), view: Literal['validated', 'provisional'] = 'validated'):
        if db.is_available():
            page_data = db.get_pathways_page(limit=limit, max_hops=max_hops, offset=offset, view=view)
            if page_data is not None:
                return page_data
        data = snapshot.read(snapshot.manifest['pathways'][view])
        if view == 'validated' and data.get('policy') in {'whole-pathway-v1','neighbor-pair-v1'}:
            records = get_validated_records()
            rows = sorted((r for r in records if (r.get('eligible') or r.get('historical_eligible')) and not r.get('stale')
                           and len(r['target']['edges']) <= max_hops), key=lambda r:r['pathway_id'])
            page = rows[offset:offset+limit]
            valid_ids = {e['id'] for r in rows for e in r['target']['edges']}
            edges = [e for e in data['graph']['edges'] if e['id'] in valid_ids]
            node_ids = {e[k] for e in edges for k in ('source','target')}
            return {**data,'paths':[r['target']['edges'] for r in page],'pathway_assessments':page,
                    'graph':{**data['graph'],'edges':edges,'nodes':[n for n in data['graph']['nodes'] if n['id'] in node_ids]},
                    'counts':{'claims':len(edges),'entities':len(node_ids),'papers':len({e['document_key'] for e in edges})},
                    'has_more':offset+limit<len(rows),'offset':offset,'limit':limit,'max_hops':max_hops}
        return {**data, **assembled_paths(data['graph'], limit, max_hops, offset)}

    @app.get('/api/{unknown:path}')
    def unknown_api(unknown: str):
        raise HTTPException(404, 'Viewer endpoint not found')

    if dist.is_dir():
        app.mount('/', StaticFiles(directory=dist, html=True), name='frontend')
    return app


app = create_app()
