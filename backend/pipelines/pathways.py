"""Explicitly ground saved claims without repeating extraction or validation."""
import json
from dataclasses import replace
from threading import Lock

from backend.config.settings import Settings
from backend.pipelines.unvalidated import publish_unvalidated_views, paused_metadata
from backend.providers.llm import OpenCodeClient, format_request_metrics, request_log_level
from backend.shared.artifacts import save_rows
from backend.shared.storage import read_jsonl, utc_now
from backend.shared.artifacts import save_json
from backend.pipelines.ontology import run_grounding
from backend.config.pipeline import POST_GROUNDING_STAGES_PAUSED


def assemble_saved_pathways(root, output, emit, thinking=None):
    settings = replace(Settings(), thinking=thinking) if thinking is not None else Settings()
    rows = read_jsonl(output / 'llm_requests.jsonl')
    request_log_lock = Lock()
    def log(metrics):
        with request_log_lock:
            rows.append({'at':utc_now(), **metrics})
            save_rows(output / 'llm_requests.jsonl', rows)
        emit(request_log_level(metrics), format_request_metrics(metrics))
    client = OpenCodeClient(settings, task='pathway_assembly', on_request=log,
        on_progress=lambda event:emit('progress', json.dumps(event)), response_dir=output / 'model_responses')
    candidates = read_jsonl(output / 'candidates.jsonl')
    if not candidates:
        raise ValueError('Extract claims from this PDF before grounding them')
    emit('info', 'Manual ontology grounding: using saved extracted claims; extraction is not repeated.')
    snapshot, grounding_summary = run_grounding(root, output, candidates, client, settings, emit)
    graph = publish_unvalidated_views(output, snapshot)
    summary_path = output / 'summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else {}
    summary.update(paused_metadata())
    summary.update(pathway_assembly={**snapshot['counts'], 'causal_chain_validated': False,
                                    'status': 'paused' if POST_GROUNDING_STAGES_PAUSED else 'completed'},
                   graph=graph['counts'], ontology_grounding={**grounding_summary, 'status': 'completed', 'assessed': True},
                   grounding_execution='manual', grounding_completed_at=utc_now(),
                   manual_grounding_model=client.model, manual_grounding_thinking=settings.task_thinking['pathway_assembly'],
                   manual_grounding_requests=client.request_metrics, pathway_validation=None,
                   source_entailed=0, source_unsupported=0, source_needs_review=0, run_status='completed')
    save_json(summary_path, summary)
    run_state_path = output / 'run_state.json'
    if run_state_path.exists():
        try:
            state = json.loads(run_state_path.read_text(encoding='utf-8'))
            now = utc_now()
            state.update(status='completed', action='ground_claims', updated_at=now, finished_at=now)
            save_json(run_state_path, state)
        except Exception:
            pass
    return {'run_status': 'completed', 'pathway_assembly': snapshot['counts'],
            'ontology_grounding': summary['ontology_grounding'], 'grounding_execution': 'manual', **paused_metadata()}
