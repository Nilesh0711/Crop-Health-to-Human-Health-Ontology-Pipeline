"""Overlap bounded retrieval lookahead with checkpointed graph-neighbour assessments."""
import json
import time
from queue import SimpleQueue, Empty
from copy import deepcopy
from backend.pipelines.external import prefetch_evidence
from backend.shared.artifacts import save_rows, save_json
from backend.shared.storage import read_jsonl
from backend.shared.cancellation import check_cancelled
from backend.shared.activity import TaggedMessage
from backend.stages.external_validation.workflow import prepare_evidence, validation_signature, reusable_evidence
from backend.stages.trust_graph.workflow import assess_group
from backend.stages.trust_graph.grouping import claim_batches, connections
from backend.stages.trust_graph.ledger import evidence_balance, scientific_signature, assessment_fingerprint
from backend.stages.trust_graph.ready import ReadyPaperJudge
from backend.stages.external_validation.aggregation import aggregate_assessment
from backend.stages.literature_retrieval.workflow import citation_tracking


def run_trust_assessment(candidates, all_candidates, provisional, output, client, reranker, settings,
                         articles_per_claim, query_translations, cache, results, emit, *, resume=False, nli_validator=None):
    existing = {r['candidate_id']: r for r in results}
    if settings.evidence_index_enabled and cache.persistent is None:
        from backend.shared.evidence_index import EvidenceIndex
        cache.persistent = EvidenceIndex(output.parent / '.evidence_index',
            max_bytes=settings.evidence_index_max_mb * 1024 * 1024, ttl_seconds=settings.evidence_index_ttl_hours * 3600)
    if reranker is not None and cache.persistent is not None:
        reranker.evidence_index = cache.persistent
    connections_by_pair = {(r['upstream_claim_id'], r['downstream_claim_id']): r
                           for r in read_jsonl(output / 'trust_connections.jsonl')} if resume else {}
    current_signatures = {c['candidate_id']: scientific_signature(c) for c in all_candidates}
    connections_by_pair = {key: row for key, row in connections_by_pair.items()
                          if all(row.get('claim_signatures', {}).get(cid) == current_signatures.get(cid)
                                 and row.get('validation_fingerprints', {}).get(cid) == assessment_fingerprint(existing.get(cid, {}))
                                 for cid in key)}
    save_rows(output / 'trust_connections.jsonl', list(connections_by_pair.values()))
    def checkpoint(result):
        results[:] = [r for r in results if r['candidate_id'] != result['candidate_id']]
        results.append(result)
        save_rows(output / 'validation_runs.jsonl', results)
        candidate = next(c for c in all_candidates if c['candidate_id'] == result['candidate_id'])
        scores[result['candidate_id']] = evidence_balance(candidate, result)
        save_rows(output / 'trust_scores.jsonl', list(scores.values()))
    save_rows(output / 'trust_scores.jsonl', [evidence_balance(c, existing.get(c['candidate_id'])) for c in all_candidates])
    before = (getattr(client, 'request_metrics', None) or {}).get('requests')
    stats = {'groups': 0, 'reused_groups': 0, 'reused_paper_assessments': 0,
             'primary_calls': 0, 'comparison_unbatched_calls': 0, 'group_timings': [],
             'paper_workers': settings.external_paper_workers, 'retrieval_lookahead': settings.external_prefetch}
    scores = {c['candidate_id']: evidence_balance(c, existing.get(c['candidate_id'])) for c in all_candidates}
    ready_events = {}
    pending_groups = []
    for group in claim_batches(candidates, provisional['graph'], output.name, settings.trust_batch_size):
        check_cancelled()
        stats['groups'] += 1
        if all(existing.get(c['candidate_id'], {}).get('validation_signature') ==
               validation_signature(c, settings, articles_per_claim, settings.external_top_chunks)
               and existing[c['candidate_id']].get('claim_input_signature') == scientific_signature(c)
               and existing[c['candidate_id']].get('validation_status') not in {None, 'pipeline_error', 'external_in_progress', 'retrieval_pending'}
               for c in group):
            stats['reused_groups'] += 1
            continue
        pending_groups.append((stats['groups'], group))
        ready_events[stats['groups']] = SimpleQueue()

    def prepare_group(item):
        number, group = item
        began = time.monotonic()
        prepared = {}
        for candidate in group:
            check_cancelled()
            cid = candidate['candidate_id']
            previous = existing.get(cid) if resume else None
            path = output / 'trust_prepared' / (cid + '.json')
            # Selected evidence is checkpointed even for the one prefetched
            # group not yet judged when another group fails or is stopped.
            if resume and path.exists() and not reusable_evidence(candidate, settings, previous, articles_per_claim, settings.external_top_chunks):
                saved = json.loads(path.read_text(encoding='utf-8'))
                if saved.get('claim_input_signature') == scientific_signature(candidate):
                    previous = {'retrieval_signature': saved.get('retrieval_signature'), 'attempts': [saved]}
            packet = prepare_evidence(candidate, client, reranker, settings, articles_per_claim, settings.external_top_chunks,
                progress=lambda m: emit('info', m), query_translation=query_translations.get(candidate['candidate_id']),
                cache=cache, resume_record=previous,
                **({'on_document': lambda doc, c=candidate: ready_events[number].put((c, deepcopy(doc)))}
                   if settings.paper_ready_judgement and settings.external_prefetch else {}))
            packet['search_paper_budget'] = articles_per_claim
            packet['claim_input_signature'] = scientific_signature(candidate)
            path.parent.mkdir(parents=True, exist_ok=True)
            save_json(path, packet)
            prepared[cid] = packet
        return prepared, round(time.monotonic() - began, 3)

    started = time.monotonic()
    def save_summary(status):
        after = (getattr(client, 'request_metrics', None) or {}).get('requests')
        stats['provider_attempts_this_run'] = after - before if isinstance(before, int) and isinstance(after, int) else None
        stats['wall_seconds'] = round(time.monotonic() - started, 3)
        save_json(output / 'trust_summary.json', {**stats, 'status': status, 'policy': 'shared-paper-trust-v1',
                   'score_kind': 'evidence_balance_not_probability', 'batch_size': settings.trust_batch_size})

    try:
        # One retrieval owner preserves GPU/ranker ownership and limits memory
        # to the active group plus one lookahead group. Cloud judgement overlaps
        # with the next group's search/fetch/rank work, not another GPU ranker.
        with prefetch_evidence(pending_groups, prepare_group, settings.external_prefetch, cancel_lookahead_on_exit=True) as packets:
            for (number, group), future in packets:
                check_cancelled()
                emit('stage', TaggedMessage(f'Trust assessment group {number}: {len(group)} related or standalone claims', 'external_validation'))
                waiting = time.monotonic()
                observed_documents, observed_records = {}, {}
                def publish_ready(candidate, document, result):
                    cid = candidate['candidate_id']
                    observed_documents.setdefault(cid, {})[document['evidence_document_id']] = document
                    for row in result['attempts'][-1].get('paper_judgements', []):
                        observed_records.setdefault(cid, {})[row['evidence_document_id']] = row
                    records = list(observed_records.get(cid, {}).values())
                    ledger = citation_tracking(candidate)
                    for reference in ledger:
                        matched = [d['evidence_document_id'] for d in observed_documents[cid].values()
                                   if reference['reference_id'] in d.get('source_reference_ids', [])]
                        if matched:
                            reference.update(status='included_in_judgement', resolved=True, fetched=True,
                                included_in_judgement=True, evidence_document_ids=matched)
                    packet = {'evidence_documents': list(observed_documents[cid].values()), 'paper_judgements': records,
                              'retrieval_pending': True, 'search_paper_budget': articles_per_claim,
                              'citation_ledger': ledger}
                    partial = aggregate_assessment(candidate, packet, [r['decision'] for r in records],
                        validation_signature(candidate, settings, articles_per_claim, settings.external_top_chunks), 'trust_graph')
                    partial.pop('candidate', None)
                    partial.update(claim_input_signature=scientific_signature(candidate), knowledge_building_mode='trust_graph')
                    partial['trust'] = evidence_balance(candidate, partial)
                    checkpoint(partial)
                ready = ReadyPaperJudge(client, settings, output / 'trust_assessments', publish_ready, existing)
                try:
                    while True:
                        while True:
                            try:
                                c, d = ready_events[number].get_nowait()
                            except Empty:
                                break
                            ready.add(c, d)
                        ready.poll()
                        if future.done():
                            prepared, prepare_seconds = future.result()
                            while True:
                                try:
                                    c, d = ready_events[number].get_nowait()
                                except Empty:
                                    break
                                ready.add(c, d)
                            break
                        time.sleep(0.02)
                    retrieval_waited = time.monotonic() - waiting
                    draining = time.monotonic()
                    ready.finish()
                    early_drain = time.monotonic() - draining
                    stats['early_paper_calls'] = stats.get('early_paper_calls', 0) + ready.calls
                    stats['early_paper_reused'] = stats.get('early_paper_reused', 0) + ready.reused
                    stats['primary_calls'] += ready.calls
                finally:
                    ready.close()
                judging = time.monotonic()
                _, checked_links, group_stats = assess_group(group, prepared, client, settings,
                    output / 'trust_assessments', links=connections(group, provisional['graph'], output.name),
                    on_checkpoint=checkpoint, progress=lambda m: emit('info', m), nli_validator=nli_validator)
                for row in checked_links:
                    connections_by_pair[(row['upstream_claim_id'], row['downstream_claim_id'])] = row
                save_rows(output / 'trust_connections.jsonl', list(connections_by_pair.values()))
                for key in ('reused_paper_assessments', 'primary_calls', 'comparison_unbatched_calls'):
                    stats[key] += group_stats[key]
                stats['group_timings'].append({'group': number, 'retrieval_prepare_wall': prepare_seconds,
                    'retrieval_wait_wall': round(retrieval_waited, 3), 'early_paper_drain_wall': round(early_drain, 3),
                    'early_paper_calls': ready.calls, 'final_paper_calls': group_stats.get('paper_calls', 0),
                    'synthesis_calls': group_stats.get('synthesis_calls', 0),
                    'assessment_wall': round(time.monotonic() - judging, 3),
                    **group_stats.get('timings_seconds', {})})
                save_summary('running')
                emit('success', TaggedMessage(f'Trust assessment group {number} completed; per-claim evidence balances saved', 'external_validation'))
    except BaseException:
        save_summary('interrupted')
        raise
    save_summary('complete')
    return stats
