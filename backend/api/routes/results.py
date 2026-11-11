"""Results routes, with application state injected for shared jobs and saved views."""

from __future__ import annotations
from fastapi import APIRouter
from typing import Any, Literal
from collections import defaultdict
from fastapi import HTTPException, Query
from fastapi.responses import FileResponse
from backend.shared.storage import read_jsonl
from backend.domain.abbreviations import collect_definitions, expand_claim_names
from backend.stages.graph_construction.graph import merge_graphs
from backend.shared.activity import display_log, display_request, research_activity
from backend.stages.source_validation.review import source_review_state
from backend.stages.pathway_assembly.paths import assembled_paths
from backend.stages.external_validation.history import connection_assessments
from backend.config.pipeline import VALIDATION_STAGES_PAUSED


class ResultsRoutes:
    def __init__(self, workspace):
        self.workspace = workspace

    def pdf_logs(self, pdf_id: str) -> list[dict[str, Any]]:
        return [display_log(entry) for entry in read_jsonl(self.workspace._output_dir(self.workspace._pdf_path(pdf_id)) / 'run_log.jsonl') if research_activity(entry)]

    def pdf_claims(self, pdf_id: str) -> dict[str, Any]:
        output = self.workspace._output_dir(self.workspace._pdf_path(pdf_id))
        candidates = read_jsonl(output / "candidates.jsonl")
        abbreviation_glossary = collect_definitions(candidates)
        results = read_jsonl(output / "validation_runs.jsonl")
        by_id = {item["candidate_id"]: item for item in results}
        revisions = read_jsonl(output / 'claim_revisions.jsonl')
        source_reviews = read_jsonl(output / 'source_review_history.jsonl')
        summary = self.workspace._read_json(output / 'summary.json', {})
        metadata = self.workspace._read_json(output / 'pipeline_checkpoint.json', {})
        pathway_only = (output / 'external_validation_target.json').exists() or summary.get('knowledge_building_mode') == 'pathway_only' or metadata.get('knowledge_building_mode') == 'pathway_only'
        path_records = []
        if pathway_only and not summary.get('validation_stages_paused'):
            from backend.stages.neighbor_validation.records import assessment_view
            path_records = assessment_view(self.workspace.ONTOLOGY_DIR, output.name)['records']
        saved_trust = {r['candidate_id']: r for r in read_jsonl(output / 'trust_scores.jsonl') if 'candidate_id' in r}
        reviews_by_candidate = defaultdict(list)
        for r in source_reviews:
            if 'candidate_id' in r:
                reviews_by_candidate[r['candidate_id']].append(r)
        revisions_by_candidate = defaultdict(list)
        for r in revisions:
            if 'candidate_id' in r:
                revisions_by_candidate[r['candidate_id']].append(r)
        participation_by_candidate = defaultdict(list)
        for row in path_records:
            entry = {'pathway_id': row['pathway_id'], 'verdict': row['verdict'], 'stale': row['stale'], 'assessment_scope': row.get('assessment_scope', 'neighboring_claims')}
            for r in row.get('target', {}).get('source_claims', []):
                if r.get('document_key') == output.name and 'claim' in r and 'candidate_id' in r['claim']:
                    participation_by_candidate[r['claim']['candidate_id']].append(entry)
        claims = []
        for candidate in candidates:
            cid = candidate['candidate_id']
            validation = None if pathway_only else by_id.get(cid)
            conflicts = self.workspace.verified_contradictions(candidate, validation)
            claims.append({'candidate': expand_claim_names(candidate, abbreviation_glossary), 'validation': validation,
                           'validation_stages_paused': VALIDATION_STAGES_PAUSED,
                           'historical_validation': by_id.get(cid), 'source_review': source_review_state(candidate),
                           'source_review_history': reviews_by_candidate.get(cid, []),
                           'trust': None if pathway_only else saved_trust.get(cid),
                           'external_validation_target': ('neighboring_claims' if (self.workspace.ONTOLOGY_DIR / '.neighbor_validation').exists() else 'whole_pathway') if pathway_only else 'historical_claim',
                           'pathway_participation': participation_by_candidate.get(cid, []),
                           'repair_eligibility': {'allowed': not VALIDATION_STAGES_PAUSED and (bool(conflicts) or self.workspace.repairable(candidate)),
                                                  'external_contradictions': conflicts},
                           'revisions': revisions_by_candidate.get(cid, [])})
        return {"claims": claims, "total": len(candidates),
                "extraction_rejections": read_jsonl(output / "extraction_rejections.jsonl"),
                "model_requests": [display_request(row) for row in read_jsonl(output / 'llm_requests.jsonl')]}

    def pdf_graph(self, pdf_id: str) -> dict[str, Any]:
        output = self.workspace._output_dir(self.workspace._pdf_path(pdf_id))
        if self.workspace._read_json(output / 'summary.json', {}).get('validation_stages_paused'):
            return self.workspace._read_json(output / 'graph.json', {'nodes': [], 'edges': [], 'counts': {}})
        if (self.workspace.ONTOLOGY_DIR / '.neighbor_validation').exists():
            graph = self.workspace._current_pathway_graph()
            document = self.workspace._output_dir(self.workspace._pdf_path(pdf_id)).name
            edges = [{**e,'id':e['candidate_id']} for e in graph['edges'] if e['document_key']==document]
            node_ids = {e[k] for e in edges for k in ('source','target')}
            return {**graph,'edges':edges,'nodes':[n for n in graph['nodes'] if n['id'] in node_ids],
                    'counts':{**graph['counts'],'edges':len(edges),'nodes':len(node_ids),
                              'supported_connections':len({p for e in edges for p in e['supported_pathway_ids']}), 'supported_pathways':0}}
        return self.workspace._read_json(self.workspace._output_dir(self.workspace._pdf_path(pdf_id)) / "graph.json",
                          {"nodes": [], "edges": [], "counts": {"nodes": 0, "edges": 0, "corroborated": 0}})

    def pdf_audit_graph(self, pdf_id: str) -> dict[str, Any]:
        return self.workspace._read_json(self.workspace._output_dir(self.workspace._pdf_path(pdf_id)) / "audit_graph.json",
                          {"nodes": [], "edges": [], "counts": {"nodes": 0, "edges": 0, "corroborated": 0}})

    def pdf_source_graph(self, pdf_id: str) -> dict[str, Any]:
        return self.workspace._read_json(self.workspace._output_dir(self.workspace._pdf_path(pdf_id)) / "source_graph.json",
                          {"nodes": [], "edges": [], "counts": {"nodes": 0, "edges": 0, "source_supported": 0}})

    def download_graphml(self, pdf_id: str) -> FileResponse:
        path = self.workspace._output_dir(self.workspace._pdf_path(pdf_id)) / "knowledge_graph.graphml"
        if not path.exists():
            raise HTTPException(status_code=404, detail="Run the pipeline before downloading GraphML")
        return FileResponse(path, media_type="application/graphml+xml", filename=path.name)

    def master_graph(self) -> dict[str, Any]:
        if (self.workspace.ONTOLOGY_DIR / '.neighbor_validation').exists():
            return self.workspace._current_pathway_graph()
        graphs = [self.workspace._read_json(path / "graph.json", {"nodes": [], "edges": []}) for path in self.workspace.ONTOLOGY_DIR.iterdir() if path.is_dir()]
        return merge_graphs(graphs)

    def _current_pathway_graph(self):
        from backend.stages.neighbor_validation.records import validated_graph
        data = self.workspace.read_assembly(self.workspace.ONTOLOGY_DIR,self.workspace.Settings().pathway_assembly_model,provisional=True)
        return validated_graph(self.workspace.ONTOLOGY_DIR,data.get('snapshot') or {'graph':{'nodes':[],'edges':[]}})

    def master_audit_graph(self) -> dict[str, Any]:
        graphs = [self.workspace._read_json(path / "audit_graph.json", {"nodes": [], "edges": []}) for path in self.workspace.ONTOLOGY_DIR.iterdir() if path.is_dir()]
        return merge_graphs(graphs)

    def master_source_graph(self) -> dict[str, Any]:
        graphs = [self.workspace._read_json(path / "source_graph.json", {"nodes": [], "edges": []}) for path in self.workspace.ONTOLOGY_DIR.iterdir() if path.is_dir()]
        return merge_graphs(graphs)

    def pdf_paths(self, pdf_id: str) -> list[Any]:
        return self.workspace._read_json(self.workspace._output_dir(self.workspace._pdf_path(pdf_id)) / "reasoning_paths.json", [])

    def global_pathways(self, limit: int = Query(default=100, ge=1, le=500),
                        max_hops: int = Query(default=6, ge=2, le=12), offset: int = Query(default=0, ge=0),
                        view: Literal['validated', 'provisional'] = 'validated'):
        if view == 'validated' and any((self.workspace.ONTOLOGY_DIR / d).exists() for d in ('.neighbor_validation','.pathway_validation')):
            from backend.stages.neighbor_validation.records import validated_paths
            return validated_paths(self.workspace.ONTOLOGY_DIR,limit=limit,max_hops=max_hops,offset=offset)
        if view == 'provisional' and VALIDATION_STAGES_PAUSED:
            data = self.workspace.read_assembly(self.workspace.ONTOLOGY_DIR, self.workspace.Settings().pathway_assembly_model, provisional=True, unvalidated=True)
        else:
            data = self.workspace.read_assembly(self.workspace.ONTOLOGY_DIR, self.workspace.Settings().pathway_assembly_model, provisional=True) if view == 'provisional' else self.workspace.read_assembly(self.workspace.ONTOLOGY_DIR, self.workspace.Settings().pathway_assembly_model)
        snapshot = data.pop('snapshot') or {}
        graph = snapshot.get('graph', {'nodes':[], 'edges':[], 'counts':{}})
        return {**data, 'graph':graph, 'counts':snapshot.get('counts', {}),
                'view': view, 'score_is_probability': False,
                'validation_stages_paused': VALIDATION_STAGES_PAUSED and view == 'provisional',
                'pathway_connections': connection_assessments(self.workspace.ONTOLOGY_DIR) if not VALIDATION_STAGES_PAUSED and view == 'provisional' and not (self.workspace.ONTOLOGY_DIR / '.pathway_validation').exists() else [],
                'completed_at':snapshot.get('completed_at'), **assembled_paths(graph, limit, max_hops, offset)}

    def pdf_pathway_assessments(self, pdf_id: str):
        from backend.stages.neighbor_validation.records import assessment_view
        return assessment_view(self.workspace.ONTOLOGY_DIR, self.workspace._output_dir(self.workspace._pdf_path(pdf_id)).name)

    def pathway_assessments(self):
        from backend.stages.neighbor_validation.records import assessment_view
        return assessment_view(self.workspace.ONTOLOGY_DIR)

    def pdf_trust(self, pdf_id: str):
        output = self.workspace._output_dir(self.workspace._pdf_path(pdf_id))
        result = {r['candidate_id']: r for r in read_jsonl(output / 'validation_runs.jsonl')}
        return {'claims': read_jsonl(output / 'trust_scores.jsonl'),
                'connections': [r for r in connection_assessments(self.workspace.ONTOLOGY_DIR) if r['document_key'] == output.name],
                'summary': self.workspace._read_json(output / 'trust_summary.json', {}), 'score_is_probability': False}


def create_router(workspace):
    views = ResultsRoutes(workspace)
    router = APIRouter()
    router.add_api_route('/api/pdfs/{pdf_id}/logs', views.pdf_logs, methods=['GET'])
    router.add_api_route('/api/pdfs/{pdf_id}/claims', views.pdf_claims, methods=['GET'])
    router.add_api_route('/api/pdfs/{pdf_id}/graph', views.pdf_graph, methods=['GET'])
    router.add_api_route('/api/pdfs/{pdf_id}/audit-graph', views.pdf_audit_graph, methods=['GET'])
    router.add_api_route('/api/pdfs/{pdf_id}/source-graph', views.pdf_source_graph, methods=['GET'])
    router.add_api_route('/api/pdfs/{pdf_id}/graph.graphml', views.download_graphml, methods=['GET'])
    router.add_api_route('/api/graph', views.master_graph, methods=['GET'])
    router.add_api_route('/api/audit-graph', views.master_audit_graph, methods=['GET'])
    router.add_api_route('/api/source-graph', views.master_source_graph, methods=['GET'])
    router.add_api_route('/api/pdfs/{pdf_id}/paths', views.pdf_paths, methods=['GET'])
    router.add_api_route('/api/pathways', views.global_pathways, methods=['GET'])
    router.add_api_route('/api/pdfs/{pdf_id}/pathway-assessments', views.pdf_pathway_assessments, methods=['GET'])
    router.add_api_route('/api/pathway-assessments', views.pathway_assessments, methods=['GET'])
    router.add_api_route('/api/pdfs/{pdf_id}/trust', views.pdf_trust, methods=['GET'])
    return router, views
