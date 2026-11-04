"""Assess local adjacent-claim pairs; never enumerate complete validation routes."""
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import RLock
import json
from backend.shared.artifacts import save_json
from backend.shared.storage import utc_now
from backend.shared.activity import TaggedMessage
from backend.shared.cancellation import (check_cancelled, submit_work, CancellationToken,
                                         cancellation_scope, current_token, PipelineStopped)
from backend.shared.cache import RunCache
from backend.stages.evidence_ranking.passages import SemanticReranker
from backend.stages.neighbor_validation.targets import POLICY, ASSESSMENT_CONTRACT, enumerate_targets, load_candidates, signature, source_signature
from backend.stages.neighbor_validation.workflow import assess_pathway, config_signature
from backend.stages.neighbor_validation.findings import FindingCache
from backend.stages.neighbor_validation.records import (save_record, save_records, read_records,
    publish_views, write_evaluation_sample, DIRECTORY)


def _checkpoint_writer(root, provisional, records):
    states = {r['pathway_id']:(r.get('verdict'),r.get('local_connection_assessed',False)) for r in records}
    lock = RLock()
    def checkpoint(record):
        with lock:
            save_record(root, record)
            state = (record.get('verdict'),record.get('local_connection_assessed',False))
            if states.get(record['pathway_id']) != state:
                publish_views(root, provisional)
                states[record['pathway_id']] = state
    return checkpoint


def run_pathway_assessment(root, output, provisional, clients, settings, emit, *, cache=None, assessment_id=None):
    (root / DIRECTORY).mkdir(parents=True,exist_ok=True)
    existing = {r['pathway_id']:r for r in read_records(root,graph=provisional['graph'])}
    incomplete = {None,'queued','in_progress','pipeline_error','assessment_incomplete'}
    reusable = {identifier for identifier,row in existing.items() if not row['stale']
        and row.get('verdict') not in incomplete
        and (row.get('verdict')!='connection_supported' or row.get('eligible'))
        and row.get('validation_signature')==config_signature(row['target'],settings,assessment_id)}
    candidates = load_candidates(root)
    state_path = root / DIRECTORY / 'selection_state.json'
    state = json.loads(state_path.read_text(encoding='utf-8')) if state_path.exists() else {}
    versions = {edge['id']:signature({'mapping':{k:edge.get(k) for k in
        ('source','target','source_label','target_label')},
        'source':source_signature(candidates.get((edge['document_key'],edge['candidate_id']),{}))})
        for edge in provisional['graph']['edges']}
    # Include changed mappings elsewhere in the global graph, not only edges
    # extracted from this PDF. Retain deferred seeds across budgeted resumes.
    has_doc_edges = any(edge['document_key']==output.name for edge in provisional['graph']['edges'])
    affected = {identifier for identifier,value in versions.items() if state.get('edge_versions',{}).get(identifier)!=value}
    affected.update(edge['id'] for edge in provisional['graph']['edges'] if edge['document_key']==output.name)
    if has_doc_edges:
        doc_edge_ids = {edge['id'] for edge in provisional['graph']['edges'] if edge['document_key']==output.name}
        affected.update(eid for eid in state.get('pending_edge_ids',[]) if eid in doc_edge_ids)
        affected.update(edge['id'] for row in existing.values()
                        if row['target'].get('assessment_contract') == ASSESSMENT_CONTRACT
                        and (row['stale'] or row.get('verdict') in incomplete)
                        and output.name in row['target'].get('document_keys', [])
                        for edge in row['target']['edges'])
    else:
        affected.update(state.get('pending_edge_ids',[]))
        affected.update(edge['id'] for row in existing.values()
                        if row['target'].get('assessment_contract') == ASSESSMENT_CONTRACT
                        and (row['stale'] or row.get('verdict') in incomplete)
                        for edge in row['target']['edges'])
    targets, coverage = enumerate_targets(provisional['graph'],candidates,
        document_key=output.name,limit=settings.neighbor_max_pairs,edge_ids=affected,completed_ids=reusable)
    other_pending = [eid for eid in state.get('pending_edge_ids',[]) if eid not in affected]
    save_json(state_path,{'edge_versions':versions,'pending_edge_ids':sorted(set(other_pending) | (affected if coverage['has_more'] else set()))})
    coverage.update(document_key=output.name,updated_at=utc_now(),
        assembly_candidate_search_exhaustive=(provisional.get('candidate_selection') or {}).get('exhaustive',True),
        pathway_coverage_is_over_assembled_graph=True)
    save_json(output / 'pathway_coverage.json',coverage)
    save_json(root / DIRECTORY / 'coverage.json',coverage)
    pending, completed, queued = [], {identifier:existing[identifier] for identifier in coverage['reused_pair_ids']}, []
    for target in targets:
        old = existing.get(target['pathway_id'],{})
        fingerprint = config_signature(target,settings,assessment_id)
        if target['pathway_id'] in reusable:
            completed[target['pathway_id']] = old
        else:
            pending.append(target)
            queued.append({'pathway_id':target['pathway_id'],'target':target,'verdict':'queued','policy':POLICY,
                'validation_signature':fingerprint,'assessment_scope':'neighboring_claims',
                'whole_chain_assessment':'not_assessed','whole_pathway_assessed':False,
                **({'assessment_id':assessment_id} if assessment_id else {}),'updated_at':utc_now()})
    if queued:
        save_records(root,queued)
    publish_views(root,provisional)
    checkpoint = _checkpoint_writer(root,provisional,read_records(root,graph=provisional['graph']))
    reused_count = len(coverage.get('reused_pair_ids', []))
    total_pairs = reused_count + len(pending)
    if reused_count > 0 and pending:
        emit('info', TaggedMessage(f"Resuming external validation: {reused_count} neighbor pairs already completed from saved checkpoints; continuing remaining {len(pending)} pairs (budget: {settings.neighbor_max_pairs})", 'external_validation'))
    elif not pending and reused_count > 0:
        emit('info', TaggedMessage(f"All {reused_count} neighbor pairs already completed from saved checkpoints; reusing validated connections", 'external_validation'))
    if coverage['has_more']:
        emit('warning',TaggedMessage('Neighbor-pair budget reached; additional local connections remain unassessed','external_validation'))
    if not targets and not coverage['reused_pair_ids']:
        emit('warning',TaggedMessage('No adjacent eligible claim pairs; standalone claims remain source-grounded only','external_validation'))
    cache = cache if cache is not None else RunCache()
    if settings.evidence_index_enabled and cache.persistent is None:
        from backend.shared.evidence_index import EvidenceIndex
        cache.persistent = EvidenceIndex(root / '.evidence_index',max_bytes=settings.evidence_index_max_mb*1024*1024,
                                         ttl_seconds=settings.evidence_index_ttl_hours*3600)
    ranker = None
    findings = FindingCache(settings,cache.persistent,assessment_id)
    if pending:
        ranker = SemanticReranker(settings)
        ranker.evidence_index = cache.persistent
        ranker.warmup()
        token = CancellationToken(current_token())
        def assess(number,target):
            pair_num = reused_count + number
            emit('stage',TaggedMessage(f"Neighbor pair {pair_num}/{total_pairs}: assessing two claims and their local connection ({reused_count} completed from previous checkpoints)",'external_validation'))
            try:
                result = assess_pathway(target,clients['query'],clients['external'],ranker,settings,cache,
                    root / DIRECTORY / 'checkpoints',checkpoint,
                    progress=lambda message:emit('info',TaggedMessage(message,'external_validation')),
                    finding_cache=findings,**({'assessment_id':assessment_id} if assessment_id else {}))
            except Exception as exc:
                if isinstance(exc, PipelineStopped):
                    raise
                fingerprint = config_signature(target, settings, assessment_id)
                result = {'pathway_id': target['pathway_id'], 'target': target, 'policy': POLICY,
                          'assessment_contract': ASSESSMENT_CONTRACT,
                          **({'assessment_id': assessment_id} if assessment_id else {}),
                          'validation_signature': fingerprint, 'verdict': 'assessment_incomplete',
                          'execution_status': 'pending', 'error': str(exc),
                          'reason': f'Neighbor evaluation encountered an error ({type(exc).__name__}: {exc}); held for retry; other pairs continue',
                          'local_connection_assessed': False, 'whole_pathway_assessed': False,
                          'assessment_scope': 'neighboring_claims', 'whole_chain_assessment': 'not_assessed',
                          'updated_at': utc_now()}
                checkpoint(result)
                emit('info', TaggedMessage(f'Neighbor pair held for retry ({type(exc).__name__}: {exc}); other pairs continue', 'external_validation'))
            emit('success',TaggedMessage(f"Neighbor outcome: {result['verdict']} (pair {pair_num}/{total_pairs})",'external_validation'))
            return result
        with cancellation_scope(token):
            with ThreadPoolExecutor(max_workers=settings.neighbor_workers,thread_name_prefix='neighbor-pair') as executor:
                pending_ids = {t['pathway_id'] for t in pending}
                futures = {submit_work(executor,assess,number,target):target['pathway_id']
                           for number,target in enumerate(targets,1) if target['pathway_id'] in pending_ids}
                try:
                    for future in as_completed(futures):
                        check_cancelled()
                        completed[futures[future]] = future.result()
                except BaseException as exc:
                    token.request(None if isinstance(exc,PipelineStopped) else
                                  'Cancelled because another neighboring-pair task failed. Saved checkpoints are preserved.')
                    for future in futures:
                        future.cancel()
                    raise
    graph = publish_views(root,provisional)
    counts = dict(Counter(r['verdict'] for r in completed.values()))
    summary = {'policy':POLICY,'assessment_scope':'neighboring_claims','whole_chain_assessment':'not_assessed',
        'assessment_contract':ASSESSMENT_CONTRACT,
        'assessed_pairs':len(completed),'assessed_pathways':0,'outcomes':counts,'coverage':coverage,
        **({'assessment_id':assessment_id} if assessment_id else {}),
        'reused_pairs':len(coverage['reused_pair_ids'])+len(targets)-len(pending),'reused_edge_findings':findings.hits,
        'standalone_claims_are_externally_unvalidated':True,'individual_external_calls':0,'graph':graph['counts'],
        'evidence_index':cache.persistent.stats() if cache.persistent else None,'retrieval_cache':cache.stats,
        'embedding_cache_hits':ranker.embedding_cache_hits if ranker else 0,
        'cross_encoder_cache_hits':getattr(ranker,'cross_encoder_cache_hits',0)}
    save_json(output / 'neighbor_validation_summary.json',summary)
    write_evaluation_sample(root,output)
    return summary
