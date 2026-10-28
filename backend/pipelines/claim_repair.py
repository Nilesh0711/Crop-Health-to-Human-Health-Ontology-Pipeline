"""Coordinate bounded claim repair and external-only recovery without changing other claims."""

from __future__ import annotations
from dataclasses import replace

import json
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable
from backend.config.settings import SCHEMA_VERSION, Settings
from backend.shared.cancellation import PipelineStopped
from backend.providers.llm import OpenCodeClient, format_request_metrics, model_reasoning_policy, request_log_level
from backend.providers.nli import NLIValidator
from backend.stages.evidence_ranking.passages import SemanticReranker
from backend.shared.storage import file_hash, read_jsonl, utc_now
from backend.stages.query_planning.planner import translate_query
from backend.stages.external_validation.workflow import validate_candidate, reusable_evidence
from backend.stages.external_validation.joint import verified_contradictions
from backend.domain.claim_context import apply_source_provenance
from backend.stages.evaluation.metrics import evaluation_sample
from backend.stages.graph_construction.graph import graph_from_candidates, reasoning_paths, write_graphml
from backend.stages.claim_extraction.extractor import extract_claims
from backend.stages.pdf_parsing.packets import iter_pdf_context_packets
from backend.stages.source_validation.checker import validate_source_claim
from backend.pipelines.trust import run_trust_assessment
from backend.shared.cache import RunCache
from backend.stages.trust_graph.projection import publish_graphs
from backend.stages.pathway_assembly.assembly import assemble_pathways
from backend.domain.vocabulary import normalize_entity
from backend.shared.artifacts import replace_row, save_json, save_rows


def repairable(candidate: dict[str, Any], validation: dict[str, Any] | None = None) -> bool:
    return (bool(verified_contradictions(candidate, validation)) or candidate.get('source_validation', {}).get('decision') in {'unsupported', 'needs_review'}
            or bool(candidate.get('repair_revision') and candidate.get('source_validation', {}).get('decision') != 'entailed'))


def refresh_saved_views(pdf: Path, output: Path) -> dict[str, Any] | None:
    """Rebuild views from saved rows, preserving unrelated claims and gold labels."""
    candidates = read_jsonl(output / 'candidates.jsonl')
    if not candidates:
        return None
    results = read_jsonl(output / 'validation_runs.jsonl')
    by_id = {row['candidate_id']: row for row in results}
    semantic = [row['semantic_validation'] for row in candidates if row.get('semantic_validation')]
    source = [row['source_validation'] for row in candidates if row.get('source_validation')]
    for name, rows in [('semantic_validation', semantic), ('source_validation', source)]:
        save_rows(output / f'{name}.jsonl', rows)
    filters = {
        'schema_valid_candidates': lambda c: not c.get('schema_error'),
        'semantic_valid_candidates': lambda c: not c.get('schema_error') and c.get('semantic_validation', {}).get('decision') == 'accepted',
        'source_entailed_candidates': lambda c: c.get('source_validation', {}).get('decision') == 'entailed',
    }
    for name, predicate in filters.items():
        save_rows(output / f'{name}.jsonl', [{'candidate_id': c['candidate_id']} for c in candidates if predicate(c)])
    graphs = {}
    for view, name in [('main', 'graph'), ('source', 'source_graph'), ('audit', 'audit_graph')]:
        graphs[name] = graph_from_candidates(candidates, results, view=view)
        save_json(output / f'{name}.json', graphs[name])
    write_graphml(graphs['graph'], output / 'knowledge_graph.graphml')
    paths = reasoning_paths(graphs['graph'])
    save_json(output / 'reasoning_paths.json', paths)
    old_evaluation = {row['candidate_id']: row for row in read_jsonl(output / 'evaluation_sample.jsonl')}
    sample = evaluation_sample(candidates, results, limit=max(80, len(old_evaluation)))
    for row in sample:
        previous = old_evaluation.get(row['candidate_id'])
        if (previous and previous.get('claim') == row.get('claim')
                and previous.get('source_evidence_span') == row.get('source_evidence_span')):
            row['gold'] = previous['gold']
    save_rows(output / 'evaluation_sample.jsonl', sample)
    summary_path = output / 'summary.json'
    summary = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else {}
    unfinished = [c['candidate_id'] for c in candidates
                  if c.get('source_validation', {}).get('decision') == 'entailed' and
                  by_id.get(c['candidate_id'], {}).get('validation_status', 'external_in_progress') in
                  {'external_in_progress', 'pipeline_error', 'retrieval_pending'}]
    requests = read_jsonl(output / 'llm_requests.jsonl')
    task_calls = {task: {'requests': len([r for r in requests if r.get('stage') == task]),
                         'elapsed_seconds': round(sum(r.get('elapsed_seconds', 0) for r in requests if r.get('stage') == task), 3)}
                  for task in Settings().task_models}
    summary.update({
        'pdf': pdf.name, 'pdf_sha256': file_hash(pdf), 'schema_version': SCHEMA_VERSION,
        'updated_at': utc_now(), 'model_policy': 'primary_only',
        'run_status': 'completed_with_warnings' if unfinished or summary.get('extraction_coverage', {}).get('complete') is False else 'completed',
        'extracted': len(candidates), 'schema_valid': sum(not c.get('schema_error') for c in candidates),
        'semantic_accepted': sum(r['decision'] == 'accepted' for r in semantic),
        'semantic_stage': 'removed' if all(r.get('decision') == 'not_run' for r in semantic) else 'historical_mixed',
        'semantic_rejected': sum(r['decision'] == 'rejected' for r in semantic),
        'semantic_needs_review': sum(r['decision'] == 'needs_review' for r in semantic),
        'semantic_type_normalized': sum(r.get('normalization_applied') is True for r in semantic),
        'source_entailed': sum(r['decision'] == 'entailed' for r in source),
        'source_unsupported': sum(r['decision'] == 'unsupported' for r in source),
        'source_needs_review': sum(r['decision'] == 'needs_review' for r in source),
        'externally_evaluated': sum(r.get('validation_status') not in {'pipeline_error', 'external_in_progress', 'retrieval_pending'} for r in results),
        'external_incomplete': len(unfinished),
        'retrieval_pending': sum(r.get('validation_status') == 'retrieval_pending' for r in results),
        'graph': graphs['graph']['counts'], 'source_graph': graphs['source_graph']['counts'],
        'audit_graph': graphs['audit_graph']['counts'], 'reasoning_paths': len(paths),
        'evaluation_sample': 'evaluation_sample.jsonl', 'models_used': sorted({r['model'] for r in requests}),
        'task_models': Settings().task_models,
        'external_phase_models': Settings().external_phase_models,
        'llm_reasoning_policy': {task: {key: next((r.get(key) for r in reversed(requests) if r.get('stage') == task and r.get(key) is not None), value) for key, value in model_reasoning_policy(model).items()} for task, model in Settings().task_models.items()},
        'llm_calls': {**task_calls, 'helper': task_calls['query'], 'validation': task_calls['external'], 'total_requests': len(requests)},
    })
    save_json(summary_path, summary)
    return summary


def run_claim_action(pdf: Path, output: Path, candidate_id: str, action: str,
                     articles_per_claim: int, emit: Callable[[str, str], None], thinking: dict[str, bool] | None = None,
                     knowledge_building_mode: str | None = None, trust_batch_size: int | None = None) -> dict[str, Any]:
    candidates = read_jsonl(output / 'candidates.jsonl')
    original = next((row for row in candidates if row['candidate_id'] == candidate_id), None)
    if original is None:
        raise ValueError('Claim not found')
    if not original.get('source_pdf_sha256') or original['source_pdf_sha256'] != file_hash(pdf):
        raise ValueError('Source PDF identity changed or is missing; start a fresh PDF run before revising this claim')
    results = read_jsonl(output / 'validation_runs.jsonl')
    before = next((r for r in results if r['candidate_id'] == candidate_id), None)
    if action == 'repair' and not repairable(original, before):
        raise ValueError('Repair requires a source review/correction or a verified external contradiction')
    if action == 'retry_external' and (original.get('schema_error') or
            original.get('source_validation', {}).get('decision') != 'entailed'):
        raise ValueError('External retry requires a schema-valid, source-entailed claim')
    if action not in {'repair', 'retry_external'}:
        raise ValueError('Unknown claim action')
    settings = replace(Settings(), thinking=thinking) if thinking is not None else Settings()
    trust_mode = knowledge_building_mode == 'trust_graph'
    if trust_mode:
        settings = replace(settings, external_judgement_mode='trust_graph', knowledge_building_mode='trust_graph',
                           trust_batch_size=trust_batch_size or settings.trust_batch_size)
    required = {'extraction', 'source', 'query', 'external'} if action == 'repair' else {'query', 'external'}
    required.add('pathway_assembly')
    available = OpenCodeClient(settings).health_check().get('models', [])
    for task in required:
        if settings.task_models[task].split('#', 1)[0] not in available:
            raise ValueError(f'Required primary unavailable for {task}: {settings.task_models[task]}')
    if settings.external_judgement_mode in {'per_paper_synthesis', 'trust_graph'} and settings.external_paper_model not in available:
        raise ValueError(f'Required per-paper primary unavailable: {settings.external_paper_model}')
    results = read_jsonl(output / 'validation_runs.jsonl')
    before = next((r for r in results if r['candidate_id'] == candidate_id), None)
    history = {'operation_id': uuid.uuid4().hex, 'candidate_id': candidate_id, 'action': action,
               'started_at': utc_now(), 'status': 'running', 'thinking': settings.task_thinking, 'original_candidate': deepcopy(original),
               'original_validation': deepcopy(before),
               'original_evaluation': next((r for r in read_jsonl(output / 'evaluation_sample.jsonl') if r['candidate_id'] == candidate_id), None)}
    history_rows = read_jsonl(output / 'claim_revisions.jsonl')
    history_rows.append(history)
    save_rows(output / 'claim_revisions.jsonl', history_rows)
    request_lock = threading.Lock()
    requests = read_jsonl(output / 'llm_requests.jsonl')

    def log_request(metrics: dict[str, Any]) -> None:
        with request_lock:
            requests.append({'at': utc_now(), 'operation_id': history['operation_id'], **metrics})
            save_rows(output / 'llm_requests.jsonl', requests)
        emit(request_log_level(metrics), format_request_metrics(metrics))

    clients = {task: OpenCodeClient(settings, settings.task_models[task], task=task, on_request=log_request,
               on_progress=lambda event: emit("progress", json.dumps(event)), response_dir=output / 'model_responses')
               for task in required}
    candidate = deepcopy(original)

    def persist_candidate() -> None:
        nonlocal candidates
        candidates = replace_row(candidates, candidate)
        save_rows(output / 'candidates.jsonl', candidates)

    def checkpoint(record: dict[str, Any]) -> None:
        nonlocal results
        record.pop('candidate', None)
        results = replace_row(results, record)
        save_rows(output / 'validation_runs.jsonl', results)

    try:
        if action == 'repair':
            emit('stage', f'Repairing only claim {candidate_id}; original judgement is retained in revision history')
            saved_contexts = read_jsonl(output / 'extraction_contexts.jsonl')
            context_packets = saved_contexts or iter_pdf_context_packets(pdf, max_pages=original.get('source_page_end', 0))
            packet = next((p for p in context_packets
                           if p['packet_number'] == original.get('source_packet_number')), None)
            if packet is None or original['source_evidence_span'] not in ' '.join(s['text'] for s in packet['sentences']):
                raise ValueError('Original source packet could not be reproduced; source repair stopped')
            target = {'claim': {k: original.get(k) for k in ('subject', 'object', 'relation', 'certainty', 'qualifiers', 'claim_text')},
                      'source_failure': original.get('source_validation'), 'verified_external_contradictions': verified_contradictions(original, before)}
            proposed, _, discards = extract_claims(pdf, clients['extraction'], 0, emit, packets=[packet],
                                                   claims_per_packet=1, repair_target=target)
            if len(proposed) != 1:
                history.update(status='no_grounded_correction', rejection_details=discards)
                emit('warning', 'No grounded correction was produced; the original claim remains available')
                return {'run_status': 'completed_with_warnings', 'candidate_id': candidate_id}
            candidate = proposed[0]
            candidate['assertion_id'] = candidate['candidate_id']
            candidate['candidate_id'] = candidate_id
            candidate['repair_revision'] = int(original.get('repair_revision', 0)) + 1
            history['proposed_candidate'] = deepcopy(candidate)
            if candidate.get('schema_error'):
                raise ValueError(f"Correction failed controlled vocabulary: {candidate['schema_error']}")
            candidate['semantic_validation'] = {'candidate_id': candidate_id, 'decision': 'not_run',
                                                 'rationale': 'Type and scope assessment moved to joint external evidence judgement'}
            for role in ('subject', 'object'):
                candidate[f'{role}_normalization'] = normalize_entity(candidate[role], candidate[f'{role}_type'])
            emit('stage', 'Rechecking corrected claim against original PDF evidence')
            from backend.stages.source_validation.context import section_context
            candidate['source_section_context'] = section_context(candidate, packet)
            source = validate_source_claim(candidate, clients['source'])
            candidate['source_validation'] = source
            apply_source_provenance(candidate, source)
            # Old external decisions describe the old assertion and cannot survive a correction.
            results = [r for r in results if r['candidate_id'] != candidate_id]
            save_rows(output / 'validation_runs.jsonl', results)
            persist_candidate()
            if candidate.get('source_validation', {}).get('decision') != 'entailed':
                history.update(status='still_needs_review', revised_candidate=deepcopy(candidate))
                emit('warning', 'Correction did not pass source fidelity; external validation was not run')
                return {'run_status': 'completed_with_warnings', 'candidate_id': candidate_id}

        nli = NLIValidator(settings) if settings.enable_nli else None
        if nli:
            nli.warmup()
        ranker = SemanticReranker(settings)
        ranker.warmup()
        resume = before if action == 'retry_external' else None
        reusable = reusable_evidence(candidate, settings, resume, articles_per_claim, settings.external_top_chunks)
        translated = None if reusable else translate_query(clients['query'], candidate)
        emit('stage', f'External validation for only {candidate_id}')
        if trust_mode:
            provisional = assemble_pathways(output.parent, clients['pathway_assembly'], settings, emit, provisional=True)
            run_trust_assessment([candidate], candidates, provisional, output, clients['external'], ranker, settings,
                                 articles_per_claim, {candidate_id: translated} if translated else {}, RunCache(), results, emit,
                                 resume=action == 'retry_external', nli_validator=nli)
            validation = next(r for r in results if r['candidate_id'] == candidate_id)
            publish_graphs(output.parent, provisional, settings)
        else:
            validation = validate_candidate(candidate, clients['external'], ranker, settings,
                                        articles_per_claim, settings.external_top_chunks, progress=lambda m: emit('info', m),
                                        nli_validator=nli, query_translation=translated,
                                        on_checkpoint=checkpoint, resume_record=resume)
        checkpoint(validation)
        if not trust_mode:
            assemble_pathways(output.parent, clients['pathway_assembly'], settings, emit)
        history.update(status='completed', revised_candidate=deepcopy(candidate), validation_status=validation['validation_status'])
        emit('success', f"Claim action completed: {validation['validation_status']}")
        return {'run_status': 'completed', 'candidate_id': candidate_id}
    except PipelineStopped:
        history.update(status='stopped', error=None, proposed_candidate=deepcopy(candidate))
        raise
    except Exception as exc:
        history.update(status='failed', error=str(exc), proposed_candidate=deepcopy(candidate))
        # A corrected, source-entailed claim remains retryable if retrieval/model setup failed before a checkpoint.
        if (action == 'repair' and candidate.get('repair_revision') and
                candidate.get('source_validation', {}).get('decision') == 'entailed' and
                not any(r['candidate_id'] == candidate_id for r in results)):
            checkpoint({'candidate_id': candidate_id, 'schema_version': SCHEMA_VERSION,
                        'validation_status': 'pipeline_error', 'error': str(exc), 'attempts': []})
        raise
    finally:
        history['finished_at'] = utc_now()
        save_rows(output / 'claim_revisions.jsonl', history_rows)
        try:
            refresh_saved_views(pdf, output)
        except Exception as exc:
            emit('error', f'Could not refresh claim views: {exc}')
            if history['status'] not in {'failed', 'stopped'}:
                raise
