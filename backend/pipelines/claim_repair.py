"""Coordinate deliberate source repair or atomic re-extraction, preserving unrelated claims."""

from __future__ import annotations
from backend.config.pipeline import VALIDATION_STAGES_PAUSED, PAUSED_MESSAGE
from dataclasses import replace

import json
import threading
import uuid
from copy import deepcopy
from pathlib import Path
from backend.stages.source_validation.summary import source_assessment_counts
from backend.stages.source_validation.review import source_fidelity_decision
from typing import Any
from backend.config.settings import SCHEMA_VERSION, Settings
from backend.shared.cancellation import PipelineStopped
from backend.providers.llm import OpenCodeClient, format_request_metrics, model_reasoning_policy, request_log_level
from backend.shared.storage import file_hash, read_jsonl, utc_now, stable_hash
from backend.stages.external_validation.history import verified_contradictions
from backend.domain.claim_context import apply_source_provenance
from backend.stages.evaluation.metrics import evaluation_sample
from backend.stages.graph_construction.graph import graph_from_candidates, reasoning_paths, write_graphml
from backend.stages.claim_extraction.extractor import extract_claims
from backend.stages.pdf_parsing.packets import iter_pdf_context_packets
from backend.stages.source_validation.checker import validate_source_claim
from backend.stages.pathway_assembly.assembly import assemble_pathways
from backend.shared.artifacts import replace_row, save_json, save_rows


def repairable(candidate: dict[str, Any], validation: dict[str, Any] | None = None) -> bool:
    return not candidate.get('superseded_by') and (bool(verified_contradictions(candidate, validation)) or source_fidelity_decision(candidate) in {'unsupported', 'needs_review'}
            or bool(candidate.get('repair_revision') and source_fidelity_decision(candidate) != 'entailed'))


def _atomic_children(original, proposed, candidates, packet, clients, settings, history):
    """Source-check children before publishing; original fields remain an audit record."""
    from backend.stages.source_validation.context import section_context
    from backend.pipelines.checkpoints import claim_stage_signature
    from backend.shared.batching import iter_parallel_judgments
    allowed = set(original.get('source_evidence_sentence_numbers') or [])
    for child in proposed:
        if (child.get('schema_error') or child.get('source_pdf_sha256')!=original['source_pdf_sha256']
                or not set(child.get('source_evidence_sentence_numbers') or [])
                or not set(child['source_evidence_sentence_numbers']) <= allowed):
            raise ValueError('Atomic child lacks matching parent sentence/PDF provenance')
        child['assertion_id'] = child['candidate_id']
        child['candidate_id'] = 'candidate-' + stable_hash(history['operation_id']+'|'+child['candidate_id'],32)
        child.update(parent_candidate_id=original['candidate_id'],reextraction_operation_id=history['operation_id'],
                     source_section_context=section_context(child,packet))
    ids = [c['candidate_id'] for c in proposed]
    if len(ids)!=len(set(ids)) or set(ids)&{c['candidate_id'] for c in candidates}:
        raise ValueError('Atomic child identifiers collide with existing claims')
    for child,result in iter_parallel_judgments(proposed,clients['source'],validate_source_claim,
            settings.validation_batch_size,keyed=True,workers=settings.source_workers):
        child['source_validation'] = result
        apply_source_provenance(child,result)
        result['input_signature'] = claim_stage_signature(child,settings.source_model,settings.task_thinking['source'])
    history.update(status='source_checked',proposed_candidates=deepcopy(proposed))
    parent = {**original,'superseded_by':ids}
    return [parent if c['candidate_id']==original['candidate_id'] else c for c in candidates]+proposed


def refresh_saved_views(pdf: Path, output: Path) -> dict[str, Any] | None:
    """Preserve paused results; historical rebuilding requires restored validation."""
    if VALIDATION_STAGES_PAUSED:
        from backend.pipelines.unvalidated import paused_metadata
        path = output / 'summary.json'
        summary = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        return {**summary, **paused_metadata(), 'source_entailed': 0,
                'source_unsupported': 0, 'source_needs_review': 0, 'pathway_validation': None}
    candidates = read_jsonl(output / 'candidates.jsonl')
    if not candidates:
        return None
    checkpoint_path = output / 'pipeline_checkpoint.json'
    metadata = json.loads(checkpoint_path.read_text(encoding='utf-8')) if checkpoint_path.exists() else {}
    if metadata.get('knowledge_building_mode') == 'pathway_only' or (output / 'external_validation_target.json').exists() or (output.parent / '.pathway_validation').exists():
        from backend.stages.neighbor_validation.records import publish_views, assessment_view
        from backend.stages.pathway_assembly.assembly import read_assembly
        data = read_assembly(output.parent,Settings().pathway_assembly_model,provisional=True)
        provisional = data.get('snapshot') or {'graph':{'nodes':[],'edges':[]}}
        publish_views(output.parent,provisional)
        path = output / 'summary.json'
        summary = json.loads(path.read_text(encoding='utf-8')) if path.exists() else {}
        records = assessment_view(output.parent,output.name)
        summary.update(knowledge_building_mode='pathway_only',external_validation_target='neighboring_claims',
            extracted=len(candidates),schema_valid=sum(not c.get('schema_error') for c in candidates), **source_assessment_counts(candidates),
            externally_evaluated=0,external_incomplete=0,pathway_validation={'outcomes':records['counts'],'coverage':records['coverage']},
            graph=json.loads((output / 'graph.json').read_text(encoding='utf-8'))['counts'])
        save_json(path,summary)
        return summary
    results = read_jsonl(output / 'validation_runs.jsonl')
    by_id = {row['candidate_id']: row for row in results}
    semantic = [row['semantic_validation'] for row in candidates if row.get('semantic_validation')]
    source = [row['source_validation'] for row in candidates if row.get('source_validation')]
    for name, rows in [('semantic_validation', semantic), ('source_validation', source)]:
        save_rows(output / f'{name}.jsonl', rows)
    filters = {
        'schema_valid_candidates': lambda c: not c.get('schema_error'),
        'semantic_valid_candidates': lambda c: not c.get('schema_error') and c.get('semantic_validation', {}).get('decision') == 'accepted',
        'source_entailed_candidates': lambda c: source_fidelity_decision(c) == 'entailed',
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
                  if source_fidelity_decision(c) == 'entailed' and
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
        **source_assessment_counts(candidates),
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


def run_claim_action(pdf, output, candidate_id, action, articles_per_pathway, emit, thinking=None,
                     neighbor_max_pairs=None):
    """Archived source correction and revalidation entry point."""
    if VALIDATION_STAGES_PAUSED:
        raise RuntimeError(PAUSED_MESSAGE)
    if action not in {'repair','reextract'}:
        raise ValueError('Individual-claim external validation is retired; resume pathway validation')
    candidates = read_jsonl(output / 'candidates.jsonl')
    original = next((c for c in candidates if c['candidate_id'] == candidate_id), None)
    if not original or original.get('source_pdf_sha256') != file_hash(pdf):
        raise ValueError('Source PDF identity changed or source claim is missing')
    if not repairable(original):
        raise ValueError('Repair requires a source-fidelity rejection or review hold')
    settings = replace(Settings(), thinking=thinking if thinking is not None else Settings().thinking,
        external_top_k=articles_per_pathway, neighbor_max_pairs=neighbor_max_pairs or Settings().neighbor_max_pairs,
        )
    history = {'operation_id':uuid.uuid4().hex, 'candidate_id':candidate_id, 'action':action,
               'status':'running', 'started_at':utc_now(), 'original_candidate':deepcopy(original),
               'original_evaluation':next((r for r in read_jsonl(output / 'evaluation_sample.jsonl') if r['candidate_id']==candidate_id),None),
               'original_validation':next((r for r in read_jsonl(output / 'validation_runs.jsonl') if r['candidate_id']==candidate_id),None)}
    histories = read_jsonl(output / 'claim_revisions.jsonl') + [history]
    requests = read_jsonl(output / 'llm_requests.jsonl')
    request_lock = threading.Lock()
    def log(metrics):
        with request_lock:
            requests.append({'at':utc_now(), 'operation_id':history['operation_id'], **metrics})
            save_rows(output / 'llm_requests.jsonl',requests)
        emit(request_log_level(metrics),format_request_metrics(metrics))
    clients = {task:OpenCodeClient(settings,task=task,on_request=log,
        on_progress=lambda e:emit('progress',json.dumps(e)),response_dir=output / 'model_responses') for task in settings.task_models}
    try:
        packets = read_jsonl(output / 'extraction_contexts.jsonl') or list(iter_pdf_context_packets(pdf, max_pages=original.get('source_page_end',0)))
        packet = next((p for p in packets if p['packet_number']==original.get('source_packet_number')),None)
        fragments = [f['text'] for f in original.get('source_evidence_fragments',[])] or [original['source_evidence_span']]
        if packet is None or any(fragment not in ' '.join(r['text'] for r in packet['sentences']) for fragment in fragments):
            raise ValueError('Original source packet could not be reproduced')
        proposed,_,discards = extract_claims(pdf,clients['extraction'],0,emit,packets=[packet],claims_per_packet=0 if action=='reextract' else 1,
            repair_target={'claim':{k:original.get(k) for k in ('subject','object','relation','certainty','qualifiers','claim_text')},
                           **({'mode':'atomic_reextraction','parent_evidence_sentence_numbers':original.get('source_evidence_sentence_numbers',[])} if action=='reextract' else {}),
                           'source_failure':original.get('source_validation')})
        if action=='reextract':
            if not proposed:
                history.update(status='no_grounded_correction',rejection_details=discards)
                return {'run_status':'completed_with_warnings'}
            candidates = _atomic_children(original,proposed,candidates,packet,clients,settings,history)
            save_rows(output / 'claim_revisions.jsonl',histories)  # Durable intent before publishing children.
            save_rows(output / 'source_validation.jsonl',[c['source_validation'] for c in candidates if c.get('source_validation')])
            save_rows(output / 'candidates.jsonl',candidates)
            metadata_path = output / 'pipeline_checkpoint.json'
            if metadata_path.exists():
                metadata = json.loads(metadata_path.read_text(encoding='utf-8'))
                metadata.update(candidate_ids=[c['candidate_id'] for c in candidates],expected_candidate_count=len(candidates))
                save_json(metadata_path,metadata)
            history.update(status='completed',child_candidate_ids=[c['candidate_id'] for c in proposed])
            from backend.stages.evaluation.metrics import write_evaluation_sample
            write_evaluation_sample(output,candidates,[],preserve_existing_labels=True)
            provisional = assemble_pathways(output.parent,clients['pathway_assembly'],settings,emit,provisional=True)
            from backend.pipelines.neighbor_validation import run_pathway_assessment
            stats = run_pathway_assessment(output.parent,output,provisional,clients,settings,emit)
            summary = refresh_saved_views(pdf,output) or {}
            summary.update(pathway_validation=stats,source_reextraction_children=sum(bool(c.get('parent_candidate_id')) for c in candidates),
                           superseded_claims=sum(bool(c.get('superseded_by')) for c in candidates))
            save_json(output / 'summary.json',summary)
            return {'run_status':'completed','child_candidate_ids':history['child_candidate_ids'],'pathway_validation':stats}
        if len(proposed)!=1:
            history.update(status='no_grounded_correction',rejection_details=discards)
            return {'run_status':'completed_with_warnings'}
        candidate = proposed[0]
        candidate.update(assertion_id=candidate['candidate_id'],candidate_id=candidate_id,
                         repair_revision=original.get('repair_revision',0)+1)
        if candidate.get('schema_error'):
            raise ValueError('Source correction failed structural checks')
        from backend.stages.source_validation.context import section_context
        candidate['source_section_context'] = section_context(candidate,packet)
        candidate['source_validation'] = validate_source_claim(candidate,clients['source'])
        apply_source_provenance(candidate,candidate['source_validation'])
        from backend.pipelines.checkpoints import claim_stage_signature
        candidate['source_validation']['input_signature'] = claim_stage_signature(candidate,settings.source_model,settings.task_thinking['source'])
        candidates = replace_row(candidates,candidate)
        save_rows(output / 'candidates.jsonl',candidates)
        save_rows(output / 'source_validation.jsonl',[c['source_validation'] for c in candidates if c.get('source_validation')])
        history.update(status='completed' if candidate['source_validation']['decision']=='entailed' else 'still_needs_review',
                       revised_candidate=deepcopy(candidate))
        from backend.stages.evaluation.metrics import write_evaluation_sample
        write_evaluation_sample(output,candidates,[],preserve_existing_labels=True)
        if candidate['source_validation']['decision'] != 'entailed':
            from backend.stages.pathway_assembly.assembly import read_assembly
            from backend.stages.neighbor_validation.records import publish_views
            data = read_assembly(output.parent,settings.pathway_assembly_model,provisional=True)
            publish_views(output.parent,data.get('snapshot') or {'graph':{'nodes':[],'edges':[]}})
            return {'run_status':'completed_with_warnings','candidate_id':candidate_id}
        provisional = assemble_pathways(output.parent,clients['pathway_assembly'],settings,emit,provisional=True)
        from backend.pipelines.neighbor_validation import run_pathway_assessment
        stats = run_pathway_assessment(output.parent,output,provisional,clients,settings,emit)
        summary = refresh_saved_views(pdf,output) or {}
        summary.update(knowledge_building_mode='pathway_only',external_validation_target='neighboring_claims',pathway_validation=stats)
        save_json(output / 'summary.json',summary)
        return {'run_status':'completed' if history['status']=='completed' else 'completed_with_warnings','pathway_validation':stats}
    except PipelineStopped:
        history.update(status='stopped')
        raise
    except Exception as exc:
        history.update(status='failed',error=str(exc))
        raise
    finally:
        history['finished_at'] = utc_now()
        save_rows(output / 'claim_revisions.jsonl',histories)
