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
from backend.stages.claim_extraction.contract import (
    CANONICAL_PREDICATE_CONTRACT_VERSION,
    CLOSED_PREDICATES,
    CLOSED_PREDICATE_SET_VERSION,
    COMPATIBLE_CLOSED_PREDICATE_SET_VERSIONS,
    SAVED_CLOSED_EXTRACTION_VERSIONS,
    compatible_closed_predicate_set,
    uses_entity_only_grounding,
)


def saved_grounding_claims(output):
    checkpoint_path = output / 'pipeline_checkpoint.json'
    checkpoint = json.loads(checkpoint_path.read_bytes()) if checkpoint_path.is_file() else {}
    coverage = read_jsonl(output / 'extraction_packets.jsonl')
    if (not checkpoint.get('extraction_complete') or not coverage
            or any(row.get('status') not in {'completed', 'cached'} for row in coverage)):
        raise ValueError('Complete bounded extraction before grounding saved claims.')
    version = checkpoint.get('extraction_contract_version')
    if (version not in SAVED_CLOSED_EXTRACTION_VERSIONS
            or not compatible_closed_predicate_set(
                checkpoint.get('predicate_set_version'), checkpoint.get('predicate_whitelist_sha256')
            )):
        raise ValueError('Entity grounding requires saved findings from a compatible bounded extraction contract.')
    claims = [row for row in read_jsonl(output / 'candidates.jsonl')
              if row.get('extraction_review_status') == 'not_flagged' and not row.get('extraction_warnings')
              and not row.get('schema_error') and not row.get('superseded_by')]
    if not claims:
        raise ValueError('This extraction has no unheld saved claims to ground.')
    ids = [row.get('candidate_id') for row in claims]
    if not all(ids) or len(set(ids)) != len(ids):
        raise ValueError('The saved extraction contains missing or duplicate claim IDs.')
    for row in claims:
        if (row.get('extraction_contract_version') != version
                or row.get('source_pdf_sha256') != checkpoint.get('pdf_sha256')):
            raise ValueError('An unheld saved finding has an incompatible source or extraction version.')
        if row.get('measured_comparison'):
            if not uses_entity_only_grounding(row) or any(key in row for key in (
                'predicate_alias', 'proposed_relation_iri', 'proposed_relation_id', 'proposed_relation_mapping'
            )):
                raise ValueError('An unheld measured observation has an incompatible extraction contract or fixed predicate.')
            continue
        predicate = CLOSED_PREDICATES.get(row.get('predicate_alias'), {})
        if (row.get('predicate_set_version') not in COMPATIBLE_CLOSED_PREDICATE_SET_VERSIONS
                or row.get('proposed_relation_mapping') != 'exact' or not predicate
                or row.get('proposed_relation_iri') != predicate['iri']
                or row.get('proposed_relation_id') != predicate['id']
                or (version >= CANONICAL_PREDICATE_CONTRACT_VERSION
                    and row.get('relation') != predicate['display_label'])):
            raise ValueError('An unheld saved claim has an incompatible fixed predicate or extraction version.')
    return claims


def assemble_saved_pathways(root, output, emit, thinking=None):
    settings = replace(Settings(), thinking=thinking) if thinking is not None else Settings()
    candidates = saved_grounding_claims(output)
    rows = read_jsonl(output / 'llm_requests.jsonl')
    request_log_lock = Lock()
    def log(metrics):
        with request_log_lock:
            rows.append({'at':utc_now(), **metrics})
            save_rows(output / 'llm_requests.jsonl', rows)
        emit(request_log_level(metrics), format_request_metrics(metrics))
    client = OpenCodeClient(settings, task='pathway_assembly', on_request=log,
        on_progress=lambda event:emit('progress', json.dumps(event)), response_dir=output / 'model_responses')
    emit('info', f'Grounding entities for all {len(candidates)} unheld saved claims.')
    snapshot, grounding_summary = run_grounding(root, output, candidates, client, settings, emit,
                                               preserve_unselected=True)
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
