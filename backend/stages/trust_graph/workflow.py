"""Checkpointed assessments shared across claims; scores never substitute for proof."""
from collections import defaultdict
from copy import deepcopy
import json
import time

from backend.shared.artifacts import save_json
from backend.shared.cancellation import check_cancelled
from backend.shared.storage import utc_now
from backend.stages.external_validation.aggregation import aggregate_assessment
from backend.stages.external_validation.workflow import validation_signature
from backend.stages.external_validation.paper_proofs import guard_paper
from backend.stages.external_validation.synthesis import _signature
from backend.stages.trust_graph.judgement import (
    TRUST_JUDGE_POLICY, paper_signature, shared_documents, judge_shared_paper,
    judge_group_synthesis, apply_synthesis, bounded_groups,
)
from backend.stages.trust_graph.ledger import evidence_balance, scientific_signature, assessment_fingerprint
from backend.stages.trust_graph.scheduling import parallel_papers


def _read(path):
    if path.exists():
        return json.loads(path.read_text(encoding='utf-8'))
    return None


def assess_group(candidates, prepared, client, settings, cache_dir, *, links=None, on_checkpoint=None,
                 progress=lambda *_: None, nli_validator=None, paper_only=False):
    """One paper can assess several fixed claims. All verdicts remain claim-local."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    links = links or []
    paper_client = client.for_model(settings.external_paper_model)
    by_id = {c['candidate_id']: c for c in candidates}
    docs = shared_documents([doc for c in candidates for doc in prepared[c['candidate_id']]['evidence_documents']])
    paper_claims = defaultdict(list)
    for candidate in candidates:
        cid = candidate['candidate_id']
        for document in prepared[cid]['evidence_documents']:
            # Content is shared, while citation origin/independence metadata stays claim-local.
            document['evidence_chunks'] = deepcopy(docs[document['evidence_document_id']]['evidence_chunks'])
            paper_claims[document['evidence_document_id']].append(candidate)
    records = {c['candidate_id']: [] for c in candidates}
    paper_order = {c['candidate_id']: {d['evidence_document_id']: n for n, d in
        enumerate(prepared[c['candidate_id']]['evidence_documents'])} for c in candidates}
    completed, reused, connections, primary_calls = [], 0, [], 0
    timings = {}

    def diagnostic(candidate, decision, previous=None):
        if previous and previous.get('nli'):
            decision['nli'] = deepcopy(previous['nli'])
        elif nli_validator is not None and decision.get('evidence'):
            from backend.providers.nli import claim_hypothesis
            try:
                decision['nli'] = {'model': settings.nli_model, 'policy': 'diagnostic_only; no trust vote',
                    'scores': nli_validator.score(' '.join(e['quoted_span'] for e in decision['evidence']), claim_hypothesis(candidate))}
            except Exception as exc:
                decision['nli'] = {'model': settings.nli_model, 'error': str(exc)[:240]}

    def publish(candidate, *, assessment=None, error=None):
        cid = candidate['candidate_id']
        packet = deepcopy(prepared[cid])
        packet['paper_judgements'] = deepcopy(records[cid])
        packet['judge_allocation'] = {'policy': TRUST_JUDGE_POLICY, 'phase_models': settings.external_phase_models,
                                      'thinking': settings.task_thinking['external']}
        assessed = {r['evidence_document_id'] for r in records[cid]}
        for reference in packet.get('citation_ledger') or []:
            if assessed.intersection(reference.get('evidence_document_ids') or []):
                reference.update(status='included_in_judgement', included_in_judgement=True)
        if assessment is not None:
            packet['joint_assessment'] = assessment
        decisions = assessment.pop('_decisions') if assessment is not None else [r['decision'] for r in records[cid]]
        if assessment is not None:
            packet['joint_assessment'] = {k: v for k, v in assessment.items() if k != '_decisions'}
        result = aggregate_assessment(candidate, packet, decisions,
                   validation_signature(candidate, settings, packet.get('search_paper_budget'), settings.external_top_chunks), 'trust_graph')
        if assessment is None and packet['evidence_documents']:
            result['validation_status'] = 'pipeline_error' if error else 'external_in_progress'
        if error:
            result.update(validation_status='pipeline_error', error=error)
        result.pop('candidate', None)
        result['claim_input_signature'] = scientific_signature(candidate)
        result['knowledge_building_mode'] = 'trust_graph'
        result['trust'] = evidence_balance(candidate, result)
        if on_checkpoint:
            on_checkpoint(deepcopy(result))
        return result

    try:
        for candidate in candidates:
            publish(candidate)
        paper_started = time.monotonic()
        jobs = []
        for identifier, members in paper_claims.items():
            pending = []
            for candidate in members:
                cid = candidate['candidate_id']
                document = next(d for d in prepared[cid]['evidence_documents'] if d['evidence_document_id'] == identifier)
                signature = paper_signature(candidate, document, paper_client.model, settings.task_thinking['external'])
                key = _signature({'policy': TRUST_JUDGE_POLICY, 'input': signature, 'source': scientific_signature(candidate)})
                previous = _read(cache_dir / (key + '.json'))
                if previous is None:
                    compatible = next((r for r in prepared[cid].get('paper_judgements') or []
                                       if r.get('input_signature') == signature and r.get('response')), None)
                    if compatible:
                        previous = {**deepcopy(compatible), 'cache_signature': key, 'policy': TRUST_JUDGE_POLICY}
                if previous and previous.get('cache_signature') == key:
                    response = previous['response']
                    decision = guard_paper(candidate, document, response['decision'])
                    decision.update(finding=response['finding'], judge=previous['decision']['judge'])
                    diagnostic(candidate, decision, previous['decision'])
                    records[cid].append({**previous, 'decision': decision})
                    save_json(cache_dir / (key + '.json'), {**previous, 'decision': decision})
                    reused += 1; publish(candidate)
                else:
                    pending.append((candidate, document, signature, key))
            for batch in bounded_groups(pending, settings.trust_batch_size):
                jobs.append((identifier, batch))

        def request_paper(job):
            identifier, batch = job
            check_cancelled()
            items = [row[0] for row in batch]
            response = judge_shared_paper(paper_client, items, docs[identifier],
                                          {c['candidate_id']: d for c, d, _, _ in batch}, settings)
            # Metrics are thread-local: capture on the actual request worker.
            return response, getattr(paper_client, 'last_request_metrics', None) or {}

        progress(f'Grouped external assessment: {len(jobs)} paper requests; up to {settings.external_paper_workers} in parallel')
        for (identifier, batch), (response, metrics) in parallel_papers(jobs, request_paper, settings.external_paper_workers):
            primary_calls += 1
            answers = {r['candidate_id']: r for r in response['results']}
            for candidate, document, signature, key in batch:
                cid = candidate['candidate_id']
                answer = {k: v for k, v in answers[cid].items() if k != 'candidate_id'}
                decision = guard_paper(candidate, document, answer['decision'])
                decision.update(finding=answer['finding'], judge={'tier': 'shared_paper',
                    'primary_model': paper_client.model, 'actual_model': metrics.get('model', paper_client.model),
                    'thinking': settings.task_thinking['external'], 'request_status': 'review' if decision['decision'] == 'needs_review' else 'ok'})
                diagnostic(candidate, decision)
                row = {'input_signature': signature, 'cache_signature': key, 'evidence_document_id': identifier,
                       'response': answer, 'decision': decision, 'policy': TRUST_JUDGE_POLICY, 'saved_at': utc_now()}
                save_json(cache_dir / (key + '.json'), row)
                records[cid].append(row)
                records[cid].sort(key=lambda r: paper_order[cid][r['evidence_document_id']])
                publish(candidate)
        timings['paper_assessment_wall'] = round(time.monotonic() - paper_started, 3)
        paper_calls = primary_calls
        if paper_only:
            return [publish(c) for c in candidates], [], {'primary_calls': primary_calls,
                'reused_paper_assessments': reused, 'timings_seconds': timings}
        active = [c for c in candidates if prepared[c['candidate_id']]['evidence_documents']]
        for candidate in candidates:
            if candidate not in active:
                completed.append(publish(candidate))
        if active:
            synthesis_started = time.monotonic()
            active_ids = {c['candidate_id'] for c in active}
            local_links = [link for link in links if {link['upstream_claim_id'], link['downstream_claim_id']} <= active_ids]
            signature = _signature({'policy': TRUST_JUDGE_POLICY, 'claims': [scientific_signature(c) for c in active],
                'records': {c['candidate_id']: records[c['candidate_id']] for c in active},
                'links': local_links, 'model': client.model, 'thinking': settings.task_thinking['external']})
            previous = _read(cache_dir / ('synthesis-' + signature + '.json'))
            if previous and previous.get('input_signature') == signature:
                response = previous['response']
            else:
                response = judge_group_synthesis(client, active, prepared, records, local_links, settings)
                primary_calls += 1
                save_json(cache_dir / ('synthesis-' + signature + '.json'),
                          {'input_signature': signature, 'response': response, 'saved_at': utc_now()})
            answers = {r['candidate_id']: r for r in response['results']}
            connections = [{**row, 'causal_chain_validated': False, 'input_signature': signature,
                             'claim_signatures': {cid: scientific_signature(by_id[cid]) for cid in
                                 (row['upstream_claim_id'], row['downstream_claim_id'])},
                             'model': client.model, 'assessed_at': utc_now()} for row in response['pathway_connections']]
            for candidate in active:
                cid = candidate['candidate_id']
                answer = {k: v for k, v in answers[cid].items() if k != 'candidate_id'}
                decisions, assessment = apply_synthesis(client, candidate, prepared[cid]['evidence_documents'], records[cid], answer, settings)
                assessment.update(policy=TRUST_JUDGE_POLICY, grouped=True, _decisions=decisions)
                completed.append(publish(candidate, assessment=assessment))
            timings['cross_paper_synthesis_wall'] = round(time.monotonic() - synthesis_started, 3)
        fingerprints = {r['candidate_id']: assessment_fingerprint(r) for r in completed}
        for row in connections:
            row['validation_fingerprints'] = {cid: fingerprints[cid] for cid in
                                               (row['upstream_claim_id'], row['downstream_claim_id'])}
        return completed, connections, {'claims': len(candidates), 'reused_paper_assessments': reused,
            'paper_calls': paper_calls, 'synthesis_calls': primary_calls - paper_calls,
            'paper_workers': settings.external_paper_workers, 'timings_seconds': timings,
            'primary_calls': primary_calls, 'comparison_unbatched_calls': sum(len(prepared[c['candidate_id']]['evidence_documents']) +
                bool(prepared[c['candidate_id']]['evidence_documents']) for c in candidates)}
    except Exception as exc:
        for candidate in candidates:
            if not any(r['candidate_id'] == candidate['candidate_id'] for r in completed):
                publish(candidate, error=str(exc))
        raise
