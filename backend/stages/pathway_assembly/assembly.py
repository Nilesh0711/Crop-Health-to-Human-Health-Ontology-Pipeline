"""MiMo-assisted global entity registry, with bounded requests and resumable shards."""
from backend.shared.cancellation import check_cancelled, PipelineStopped
import json

from backend.shared.artifacts import save_json
from backend.shared.storage import stable_hash, utc_now
from backend.stages.pathway_assembly.candidates import EndpointCandidateIndex, matching_settings, matching_signature, SMALL_CONTEXT_CHARACTERS
from backend.stages.pathway_assembly.contract import SYSTEM
from backend.stages.pathway_assembly.protocol import IdentityPacket, REQUEST_PROTOCOL, check_groups
from backend.stages.pathway_assembly.corpus import (
    collect_corpus, corpus_signature, ASSEMBLY_VERSION, ASSEMBLY_DIRECTORY, ASSEMBLY_ELIGIBILITY_POLICY, PROVISIONAL_ELIGIBILITY_POLICY,
)


def _read(path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def _profile(node, by_id, context_ids):
    # Preserve every occurrence context of selected nodes, including low-ranked
    # contexts. Candidate retrieval never authorizes dropping selected proof.
    examples = []
    for identifier in context_ids:
        row = by_id[identifier]
        example = {k:row.get(k, '') for k in ('name', 'source_mention', 'paper', 'section', 'evidence', 'qualifiers', 'local_context', 'claim_text')}
        if example not in examples:
            examples.append(example)
    return dict(node_id=node['id'], canonical_label=node['label'], aliases=node['aliases'],
                entity_types=node['types'], identity_context=examples,
                occurrence_count=len(node['endpoint_ids']))


def _apply(groups, batch, registry, assignments, redirects):
    incoming = {r['endpoint_id']: r for r in batch}
    for group in groups:
        old = group['existing_node_ids']
        identifiers = set(group['endpoint_ids'])
        aliases, types, notes = set(), set(), []
        for node_id in old:
            node = registry.pop(node_id)
            identifiers.update(node['endpoint_ids'])
            aliases.update(node['aliases'])
            types.update(node['types'])
            notes.extend(node.get('alignment_notes', []))
        for endpoint_id in group['endpoint_ids']:
            aliases.add(incoming[endpoint_id]['name'])
            types.add(incoming[endpoint_id]['entity_type'])
        # Stable existing IDs survive additions; all occurrences follow a merge.
        node_id = min(old) if old else 'entity-' + stable_hash('|'.join(sorted(identifiers)), 28)
        aliases.add(group['canonical_label'])
        notes.append(group['explanation'])
        registry[node_id] = dict(id=node_id, label=group['canonical_label'], aliases=sorted(aliases),
                                 types=sorted(types), endpoint_ids=sorted(identifiers),
                                 alignment_notes=list(dict.fromkeys(notes)))
        for endpoint_id in identifiers:
            assignments[endpoint_id] = node_id
        for previous in old:
            redirects[previous] = node_id


def _resolve(node_id, redirects):
    visited = set()
    while node_id in redirects and redirects[node_id] != node_id:
        if node_id in visited:
            raise ValueError('Cyclic node redirect in pathway checkpoint')
        visited.add(node_id)
        node_id = redirects[node_id]
    return node_id


def _unchanged_occurrences(previous, endpoints):
    """Adding claim_text to model input must not hide unchanged historical views."""
    current = {row['endpoint_id']: row for row in endpoints}
    edges = {row['id']: row for row in previous.get('graph', {}).get('edges', [])}
    unchanged = set()
    for old in previous.get('endpoints', []):
        new = current.get(old['endpoint_id'])
        if new == old:
            unchanged.add(old['endpoint_id'])
        elif (new and 'claim_text' not in old
              and {k: v for k, v in new.items() if k != 'claim_text'} == old
              and edges.get(old['edge_id'], {}).get('claim_text', '') == new.get('claim_text', '')):
            unchanged.add(old['endpoint_id'])
    return unchanged


def _registry_fragments(registry, selected, redirects):
    ids = sorted({_resolve(identifier, redirects) for identifier in selected})
    return [dict(node_id=node_id, context_ids=contexts[start:start + 8])
            for node_id in ids for contexts in [registry[node_id]['endpoint_ids']]
            for start in range(0, len(contexts), 8)]


def _small_registry_fits(batch, registry, assignments, by_id, settings):
    if len(registry) > settings.pathway_candidate_small_registry:
        return False
    profiles = {node_id: _profile(node, by_id, node['endpoint_ids']) for node_id, node in registry.items()}
    # Avoid a second judgement/embedding warmup when the entire small registry is
    # already cheap. A few entities with very long contexts still use retrieval.
    return IdentityPacket(batch, profiles, assignments).input_characters(SYSTEM) <= min(
        settings.pathway_assembly_input_characters, SMALL_CONTEXT_CHARACTERS)


def _selection_coverage(state, settings):
    history = state.get('selection_history', [])
    inherited = state.get('inherited_candidate_coverage') or {}
    return {**matching_settings(settings), 'index': state.get('candidate_index'),
        'batches': len(history), 'exhaustive': inherited.get('exhaustive', True) and all(row['exhaustive'] for row in history),
        'bounded_batches': sum(not row['exhaustive'] for row in history),
        'registry_entities_considered': sum(row['registry_entities_available'] for row in history),
        'registry_entities_selected': sum(row['registry_entities_selected'] for row in history),
        'history': history, 'similarity_is_identity_proof': False,
        'inherited_candidate_coverage': inherited or None,
        'reused_endpoint_count': state.get('reused_endpoint_count', 0),
        'candidate_recall_is_verified': False}


def _scan_batch(batch, registry, assignments, redirects, by_id, state, state_path, client, settings, emit):
    if state.get('scan_complete'):
        return
    frozen = state['shard_ids']
    while True:
        check_cancelled()
        start = state['shard_cursor']
        provisional_ids = {assignments[r['endpoint_id']] for r in batch if r['endpoint_id'] in assignments}
        chosen = {node_id: _profile(registry[node_id], by_id,
            [r['endpoint_id'] for r in batch if assignments.get(r['endpoint_id']) == node_id])
            for node_id in sorted(provisional_ids)}
        end = start
        def packet():
            return IdentityPacket(batch, chosen, assignments)
        if packet().input_characters(SYSTEM) > settings.pathway_assembly_input_characters:
            raise ValueError('Pathway Assembly incoming batch/provisional registry exceeds input budget; lower PATHWAY_ASSEMBLY_BATCH_SIZE')
        while end < len(frozen):
            fragment = frozen[end]
            node_id = _resolve(fragment['node_id'], redirects)
            previous_profile = chosen.get(node_id)
            profile = _profile(registry[node_id], by_id, fragment['context_ids'])
            if previous_profile:
                profile['identity_context'] = previous_profile['identity_context'] + [
                    example for example in profile['identity_context'] if example not in previous_profile['identity_context']]
            chosen[node_id] = profile
            if packet().input_characters(SYSTEM) > settings.pathway_assembly_input_characters:
                if previous_profile:
                    chosen[node_id] = previous_profile
                else:
                    chosen.pop(node_id)
                break
            end += 1
        if end == start and end < len(frozen):
            ids = frozen[end]['context_ids']
            if len(ids) > 1:
                middle = len(ids) // 2
                frozen[end:end + 1] = [dict(node_id=frozen[end]['node_id'], context_ids=part)
                                      for part in (ids[:middle], ids[middle:])]
                continue
            raise ValueError('One pathway registry profile exceeds the remaining input budget; reduce batch size or increase assembly input budget')
        request = packet()
        emit('info', f"Pathway Assembly new/changed endpoints {state['cursor'] + 1}-{state['cursor'] + len(batch)}; registry scan {start}-{end}/{len(frozen)}")
        answer = client.chat_json(SYSTEM, request.user, max_tokens=6000, json_schema=request.schema,
            request_label=f"global pathway assembly endpoints {state['cursor'] + 1}-{state['cursor'] + len(batch)}; registry {start}-{end}",
            response_check=request.groups)
        groups = request.groups(answer)
        _apply(groups, batch, registry, assignments, redirects)
        state.update(shard_cursor=end, scan_complete=end >= len(frozen), updated_at=utc_now())
        save_json(state_path, state)
        if end >= len(frozen):
            return


def assemble_pathways(root, client, settings, emit=lambda *args: None, *, provisional=False):
    """Read every saved document and align source/external-supported edges.

    Indexed retrieval proposes context/name/lexical candidates; MiMo still decides
    identity from full source context. Exhaustive scans remain a benchmark option.
    Selected-node contexts are sharded losslessly. A failed/stopped scan never
    publishes a partial graph; bounded candidate coverage is saved explicitly.
    """
    folder = root / ('.provisional_pathways' if provisional else ASSEMBLY_DIRECTORY)
    eligibility = PROVISIONAL_ELIGIBILITY_POLICY if provisional else ASSEMBLY_ELIGIBILITY_POLICY
    folder.mkdir(parents=True, exist_ok=True)
    edges, endpoints = collect_corpus(root, provisional=provisional)
    signature = corpus_signature(edges, endpoints, settings.pathway_assembly_model, settings.task_thinking['pathway_assembly'], provisional=provisional)
    snapshot_path = folder / 'snapshot.json'
    previous = _read(snapshot_path)
    match_signature = matching_signature(settings)
    if previous and previous.get('input_signature') == signature and previous.get('matching_signature') == match_signature:
        emit('info', 'Pathway Assembly: reusing the matching global snapshot')
        return previous
    state_path = folder / 'checkpoint.json'
    state = _read(state_path)
    if not state or state.get('input_signature') != signature or state.get('matching_signature') != match_signature:
        registry, assignments, inherited_coverage = {}, {}, None
        if (previous and previous.get('version') == ASSEMBLY_VERSION
                and previous.get('model') == settings.pathway_assembly_model
                and previous.get('thinking', True) == settings.task_thinking['pathway_assembly']
                and previous.get('matching_signature') in (None, match_signature)):
            unchanged = _unchanged_occurrences(previous, endpoints)
            for node in previous['graph']['nodes']:
                kept = [identifier for identifier in node['endpoint_ids'] if identifier in unchanged]
                if kept:
                    registry[node['id']] = {**node, 'endpoint_ids': kept}
                    assignments.update({identifier:node['id'] for identifier in kept})
            if assignments and previous.get('candidate_selection'):
                inherited_coverage = {
                    'input_signature': previous['input_signature'],
                    'exhaustive': previous['candidate_selection'].get('exhaustive', True),
                    'reused_endpoint_count': len(assignments),
                }
        state = dict(input_signature=signature, matching_signature=match_signature, selection_history=[],
                     inherited_candidate_coverage=inherited_coverage,
                     reused_endpoint_count=len(assignments), thinking=settings.task_thinking['pathway_assembly'], registry=registry, assignments=assignments, redirects={}, cursor=0,
                     pending_ids=[r['endpoint_id'] for r in endpoints if r['endpoint_id'] not in assignments],
                     shard_cursor=0, shard_ids=None, batch_size=settings.pathway_assembly_batch_size)
    by_id = {r['endpoint_id']: r for r in endpoints}
    registry, assignments, redirects = (state[k] for k in ('registry', 'assignments', 'redirects'))
    pending = [by_id[identifier] for identifier in state['pending_ids']]
    state.update(status='running', error=None, updated_at=utc_now(), request_protocol=REQUEST_PROTOCOL)
    save_json(state_path, state)
    emit('stage', f"Pathway Assembly: {len(edges)} {'provisional' if provisional else 'validated'} claims across {len({e['document_key'] for e in edges})} papers; MiMo Flash aligns endpoints globally")
    try:
        candidate_index = None
        while state['cursor'] < len(pending):
            check_cancelled()
            batch = pending[state['cursor']:state['cursor'] + state['batch_size']]
            if not state.get('selection_plan'):
                use_index = (settings.pathway_candidate_strategy == 'indexed'
                             and not _small_registry_fits(batch, registry, assignments, by_id, settings))
                if use_index and candidate_index is None:
                    candidate_index = EndpointCandidateIndex(endpoints, state['pending_ids'], settings, root)
                    state['candidate_index'] = candidate_index.metadata
                    save_json(folder / 'candidate_index.json', candidate_index.metadata)
                selected, evidence = (candidate_index.select(batch, assignments, registry, settings.pathway_candidate_top_k)
                                      if use_index else (set(registry), {}))
                # Without an exact-name candidate already in the registry, widen
                # retrieval before the first judgement. This avoids spending a
                # second LLM call on nearly every genuinely new entity. An exact
                # name is only a retrieval signal, never an identity acceptance.
                broad = [row for row in batch if use_index and not any(
                    hit['node_id'] in registry and any('exact_name' in signal for signal in hit['signals'])
                    for hit in evidence[row['endpoint_id']])]
                upfront_evidence = {}
                if broad:
                    wider, upfront_evidence = candidate_index.select(broad, assignments, registry, settings.pathway_candidate_expansion_k)
                    selected |= wider
                state['selection_plan'] = {'available_nodes': sorted(registry),
                    'indexed': use_index,
                    'upfront_expansion_endpoint_ids': [row['endpoint_id'] for row in broad],
                    'upfront_expansion_candidates': upfront_evidence,
                    'prior_endpoint_ids': sorted(assignments), 'selected_nodes': sorted(selected),
                    'endpoint_ids': [row['endpoint_id'] for row in batch],
                    'base_candidates': evidence, 'round': 0}
                state['shard_ids'] = _registry_fragments(registry, selected, redirects)
                state['shard_cursor'] = 0
                state['scan_complete'] = False
                save_json(state_path, state)
            plan = state['selection_plan']
            if plan['indexed'] and candidate_index is None:
                candidate_index = EndpointCandidateIndex(endpoints, state['pending_ids'], settings, root)
            active_batch = ([row for row in batch if row['endpoint_id'] in plan['expansion_endpoint_ids']]
                            if plan['round'] else batch)
            _scan_batch(active_batch, registry, assignments, redirects, by_id, state, state_path, client, settings, emit)
            if plan['indexed'] and plan['round'] == 0:
                prior_ids = set(plan['prior_endpoint_ids'])
                unmatched = [row for row in batch if row['endpoint_id'] not in plan.get('upfront_expansion_endpoint_ids', [])
                    if not (set(registry[assignments[row['endpoint_id']]]['endpoint_ids']) & prior_ids)]
                extra, evidence = candidate_index.select(unmatched, assignments, registry, settings.pathway_candidate_expansion_k)
                # Identify original registry nodes through any redirects created by the base scan.
                expanded = {identifier for identifier in plan['available_nodes']
                            if _resolve(identifier, redirects) in extra} - set(plan['selected_nodes'])
                if expanded:
                    plan.update(round=1, expansion_candidates=evidence,
                                expansion_endpoint_ids=[row['endpoint_id'] for row in unmatched],
                                selected_nodes=sorted(set(plan['selected_nodes']) | expanded))
                    state.update(shard_ids=_registry_fragments(registry, expanded, redirects), shard_cursor=0, scan_complete=False)
                    save_json(state_path, state)
                    continue
            unsearched = sorted(set(plan['available_nodes']) - set(plan['selected_nodes']))
            state.setdefault('selection_history', []).append({
                **{k: v for k, v in plan.items() if k not in {'available_nodes', 'prior_endpoint_ids'}},
                'registry_entities_available': len(plan['available_nodes']),
                'registry_entities_selected': len(plan['selected_nodes']),
                'unsearched_registry_entities': len(unsearched), 'exhaustive': not unsearched,
                'similarity_is_identity_proof': False})
            if unsearched:
                emit('warning', f"Pathway candidate search was bounded for {len(batch)} endpoints: "
                     f"{len(unsearched)} registry entities were not compared by the model; missing links remain possible")
            state.update(cursor=state['cursor'] + len(batch), shard_cursor=0, shard_ids=None,
                         selection_plan=None, scan_complete=False)
            save_json(state_path, state)
        graph_edges = []
        for edge in edges:
            source, target = (assignments[f"{edge['id']}:{role}"] for role in ('subject', 'object'))
            graph_edges.append({**edge, 'source':source, 'target':target,
                                'source_label':registry[source]['label'], 'target_label':registry[target]['label']})
        snapshot = dict(version=ASSEMBLY_VERSION, input_signature=signature, completed_at=utc_now(),
                        request_protocol=REQUEST_PROTOCOL, matching_signature=match_signature,
                        candidate_selection=_selection_coverage(state, settings),
                        eligibility_policy=eligibility,
                        model=settings.pathway_assembly_model, thinking=settings.task_thinking['pathway_assembly'], causal_chain_validated=False,
                        graph=dict(nodes=[{**n, 'type':n['types'][0]} for n in registry.values()], edges=graph_edges,
                                   counts=dict(nodes=len(registry), edges=len(graph_edges))),
                        endpoints=endpoints, counts=dict(claims=len(edges), endpoints=len(endpoints), entities=len(registry),
                            papers=len({e['document_key'] for e in edges})))
        save_json(snapshot_path, snapshot)
        state.update(status='completed', updated_at=utc_now())
        save_json(state_path, state)
        emit('success', f"Pathway Assembly: {len(edges)} original edges connected through {len(registry)} global entities")
        return snapshot
    except PipelineStopped:
        state.update(status='stopped', error=None, updated_at=utc_now())
        save_json(state_path, state)
        raise
    except Exception as exc:
        state.update(status='failed', error=str(exc), updated_at=utc_now())
        save_json(state_path, state)
        emit('error', f'Pathway Assembly failed: {exc}')
        raise


_assembly_cache = {}


def _stat_sig(path):
    if not path.exists():
        return None
    s = path.stat()
    return (s.st_mtime_ns, s.st_size)


def read_assembly(root, model, *, provisional=False):
    from copy import deepcopy
    folder = root / ('.provisional_pathways' if provisional else ASSEMBLY_DIRECTORY)
    cand_paths = sorted(root.glob('*/candidates.jsonl'))
    val_paths = [] if provisional else sorted(root.glob('*/validation_runs.jsonl'))
    stamp = (
        _stat_sig(folder / 'snapshot.json'),
        _stat_sig(folder / 'checkpoint.json'),
        tuple((str(p), _stat_sig(p)) for p in cand_paths),
        tuple((str(p), _stat_sig(p)) for p in val_paths),
    )
    cache_key = (str(root.resolve()), model, provisional)
    cached = _assembly_cache.get(cache_key)
    if cached and cached['stamp'] == stamp:
        return deepcopy(cached['result'])
    snapshot = _read(folder / 'snapshot.json')
    state = _read(folder / 'checkpoint.json') or {}
    edges, endpoints = collect_corpus(root, provisional=provisional)
    current = corpus_signature(edges, endpoints, model, (snapshot or state).get('thinking', True), provisional=provisional)
    stale = bool(snapshot and snapshot.get('input_signature') != current)
    if stale and not snapshot.get('matching_signature'):
        # v44 stored claim_text on edges but not endpoint input objects. Compare
        # the old representation too, without relaxing any actual source field.
        old_ids = {r['endpoint_id'] for r in snapshot.get('endpoints', []) if 'claim_text' not in r}
        compatible = _unchanged_occurrences(snapshot, endpoints)
        legacy = [{k: v for k, v in row.items() if k != 'claim_text'}
                  if row['endpoint_id'] in old_ids & compatible else row for row in endpoints]
        stale = snapshot.get('input_signature') != corpus_signature(edges, legacy, model,
            (snapshot or state).get('thinking', True), provisional=provisional)
    if snapshot:
        # Never display newly excluded edges through an older saved snapshot.
        # Rebuilding identity alignment remains an explicit assembly operation.
        allowed = {edge['id']: edge for edge in edges}
        unchanged = _unchanged_occurrences(snapshot, endpoints)
        graph = snapshot['graph']
        kept = [{**edge, **allowed[edge['id']]} for edge in graph['edges']
                if edge['id'] in allowed and all(f"{edge['id']}:{role}" in unchanged
                                                for role in ('subject', 'object'))]
        endpoint_ids = {f"{edge['id']}:{role}" for edge in kept for role in ('subject', 'object')}
        node_ids = {edge[role] for edge in kept for role in ('source', 'target')}
        nodes = [{**node, 'endpoint_ids': [eid for eid in node['endpoint_ids'] if eid in endpoint_ids]}
                 for node in graph['nodes'] if node['id'] in node_ids]
        snapshot = {**snapshot, 'graph': {**graph, 'nodes': nodes, 'edges': kept,
                    'counts': {'nodes': len(nodes), 'edges': len(kept)}},
                    'endpoints': [row for row in snapshot.get('endpoints', []) if row['endpoint_id'] in endpoint_ids],
                    'counts': {'claims': len(kept), 'endpoints': len(kept) * 2,
                               'entities': len(nodes), 'papers': len({edge['document_key'] for edge in kept})}}
    relevant = state if state.get('input_signature') == current else {}
    status = ('completed' if snapshot and not stale else relevant.get('status', 'outdated' if stale else 'not_run'))
    result = dict(status=status, error=relevant.get('error'), stale=stale,
                  snapshot=snapshot, current_signature=current,
                  candidate_selection=(snapshot or {}).get('candidate_selection'))
    _assembly_cache[cache_key] = {'stamp': stamp, 'result': deepcopy(result)}
    return result
