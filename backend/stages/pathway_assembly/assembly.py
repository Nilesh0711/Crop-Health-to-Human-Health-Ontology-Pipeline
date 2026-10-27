"""MiMo-assisted global entity registry, with bounded requests and resumable shards."""
from backend.shared.cancellation import check_cancelled, PipelineStopped
import json

from backend.providers.structured_output import IncompleteResponseError
from backend.shared.artifacts import save_json
from backend.shared.storage import stable_hash, utc_now
from backend.stages.pathway_assembly.contract import SYSTEM, RESPONSE_SCHEMA
from backend.stages.pathway_assembly.corpus import (
    collect_corpus, corpus_signature, ASSEMBLY_VERSION, ASSEMBLY_DIRECTORY, ASSEMBLY_ELIGIBILITY_POLICY,
)


def _read(path):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else None


def check_groups(answer, endpoints, registry_ids, assignments=None):
    expected = {row['endpoint_id'] for row in endpoints}
    seen, used = set(), set()
    for group in answer['groups']:
        incoming, old = group['endpoint_ids'], group['existing_node_ids']
        if (not incoming and not old) or not group['canonical_label'].strip():
            raise IncompleteResponseError('An identity group is empty or unnamed')
        if (len(set(incoming)) != len(incoming) or len(set(old)) != len(old)
                or not set(incoming) <= expected or not set(old) <= registry_ids
                or seen.intersection(incoming) or used.intersection(old)):
            raise IncompleteResponseError('Invented, duplicated or mismatched pathway endpoint/node IDs')
        seen.update(incoming)
        used.update(old)
        if assignments and any(assignments.get(identifier) not in (None, *old) for identifier in incoming):
            raise IncompleteResponseError('Retain each incoming endpoint provisional node when merging registry shards')
    if seen != expected:
        raise IncompleteResponseError('Every supplied pathway endpoint must be assigned exactly once')


def _profile(node, by_id, context_ids):
    # Occurrence contexts are sharded, never silently discarded. Every frozen
    # registry occurrence is visited when comparing a new batch globally.
    examples = []
    for identifier in context_ids:
        row = by_id[identifier]
        example = {k:row[k] for k in ('name', 'source_mention', 'paper', 'section', 'evidence', 'qualifiers', 'local_context')}
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


def assemble_pathways(root, client, settings, emit=lambda *args: None):
    """Read every saved document and align source/external-supported edges.

    Each incoming batch scans all frozen registry shards, plus its provisional
    assignments. There is no top-k alias filter or isolated per-paper assembly.
    Checkpoints record batch/shard progress; an error never publishes a partial
    registry as a completed global snapshot.
    """
    folder = root / ASSEMBLY_DIRECTORY
    request_overhead = len(SYSTEM) + len(json.dumps(RESPONSE_SCHEMA, ensure_ascii=False))
    folder.mkdir(parents=True, exist_ok=True)
    edges, endpoints = collect_corpus(root)
    signature = corpus_signature(edges, endpoints, settings.pathway_assembly_model, settings.task_thinking['pathway_assembly'])
    snapshot_path = folder / 'snapshot.json'
    previous = _read(snapshot_path)
    if previous and previous.get('input_signature') == signature:
        emit('info', 'Pathway Assembly: reusing the matching global snapshot')
        return previous
    state_path = folder / 'checkpoint.json'
    state = _read(state_path)
    if not state or state.get('input_signature') != signature:
        registry, assignments = {}, {}
        by_id = {r['endpoint_id']: r for r in endpoints}
        if previous and previous.get('version') == ASSEMBLY_VERSION and previous.get('model') == settings.pathway_assembly_model and previous.get('thinking', True) == settings.task_thinking['pathway_assembly']:
            unchanged = {r['endpoint_id'] for r in previous.get('endpoints', []) if by_id.get(r['endpoint_id']) == r}
            for node in previous['graph']['nodes']:
                kept = [identifier for identifier in node['endpoint_ids'] if identifier in unchanged]
                if kept:
                    registry[node['id']] = {**node, 'endpoint_ids': kept}
                    assignments.update({identifier:node['id'] for identifier in kept})
        state = dict(input_signature=signature, thinking=settings.task_thinking['pathway_assembly'], registry=registry, assignments=assignments, redirects={}, cursor=0,
                     pending_ids=[r['endpoint_id'] for r in endpoints if r['endpoint_id'] not in assignments],
                     shard_cursor=0, shard_ids=None, batch_size=settings.pathway_assembly_batch_size)
    by_id = {r['endpoint_id']: r for r in endpoints}
    registry, assignments, redirects = (state[k] for k in ('registry', 'assignments', 'redirects'))
    pending = [by_id[identifier] for identifier in state['pending_ids']]
    state.update(status='running', error=None, updated_at=utc_now())
    save_json(state_path, state)
    emit('stage', f"Pathway Assembly: {len(edges)} validated claims across {len({e['document_key'] for e in edges})} papers; MiMo Flash aligns endpoints globally")
    try:
        while state['cursor'] < len(pending):
            check_cancelled()
            batch = pending[state['cursor']:state['cursor'] + state['batch_size']]
            if state.get('batch_scan_complete'):
                state.update(cursor=state['cursor'] + len(batch), shard_cursor=0, shard_ids=None,
                             batch_scan_complete=False)
                save_json(state_path, state)
                continue
            if state['shard_ids'] is None:
                fragments = []
                for node_id in sorted(registry):
                    ids = registry[node_id]['endpoint_ids']
                    fragments.extend(dict(node_id=node_id, context_ids=ids[start:start + 8])
                                     for start in range(0, len(ids), 8))
                state.update(shard_ids=fragments, shard_cursor=0)
            frozen = state['shard_ids']
            while True:
                start = state['shard_cursor']
                provisional = {assignments[r['endpoint_id']] for r in batch if r['endpoint_id'] in assignments}
                chosen = {node_id:_profile(registry[node_id], by_id,
                    [r['endpoint_id'] for r in batch if assignments.get(r['endpoint_id']) == node_id])
                    for node_id in sorted(provisional)}
                end = start
                def packet():
                    return json.dumps(dict(incoming_endpoints=[{**r, 'provisional_node_id':assignments.get(r['endpoint_id'])}
                                                               for r in batch], registry_nodes=list(chosen.values())), ensure_ascii=False)
                if len(packet()) + request_overhead > settings.pathway_assembly_input_characters:
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
                    if len(packet()) + request_overhead > settings.pathway_assembly_input_characters:
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
                user = packet()
                emit('info', f"Pathway Assembly new/changed endpoints {state['cursor'] + 1}-{state['cursor'] + len(batch)}/{len(pending)}; registry scan {start}-{end}/{len(frozen)}")
                answer = client.chat_json(SYSTEM, user, max_tokens=6000, json_schema=RESPONSE_SCHEMA,
                    request_label=f"global pathway assembly endpoints {state['cursor'] + 1}-{state['cursor'] + len(batch)}; registry {start}-{end}",
                    response_check=lambda value:check_groups(value, batch, set(chosen), assignments))
                check_groups(answer, batch, set(chosen), assignments)
                _apply(answer['groups'], batch, registry, assignments, redirects)
                state.update(shard_cursor=end, batch_scan_complete=end >= len(frozen), updated_at=utc_now())
                save_json(state_path, state)
                if end >= len(frozen):
                    break
            state.update(cursor=state['cursor'] + len(batch), shard_cursor=0, shard_ids=None, batch_scan_complete=False)
            save_json(state_path, state)
        graph_edges = []
        for edge in edges:
            source, target = (assignments[f"{edge['id']}:{role}"] for role in ('subject', 'object'))
            graph_edges.append({**edge, 'source':source, 'target':target,
                                'source_label':registry[source]['label'], 'target_label':registry[target]['label']})
        snapshot = dict(version=ASSEMBLY_VERSION, input_signature=signature, completed_at=utc_now(),
                        eligibility_policy=ASSEMBLY_ELIGIBILITY_POLICY,
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


def read_assembly(root, model):
    snapshot = _read(root / ASSEMBLY_DIRECTORY / 'snapshot.json')
    state = _read(root / ASSEMBLY_DIRECTORY / 'checkpoint.json') or {}
    edges, endpoints = collect_corpus(root)
    current = corpus_signature(edges, endpoints, model, (snapshot or state).get('thinking', True))
    stale = bool(snapshot and snapshot.get('input_signature') != current)
    if snapshot:
        # Never display newly excluded edges through an older saved snapshot.
        # Rebuilding identity alignment remains an explicit assembly operation.
        allowed = {edge['id']: edge for edge in edges}
        current_endpoints = {row['endpoint_id']: row for row in endpoints}
        unchanged = {row['endpoint_id'] for row in snapshot.get('endpoints', [])
                     if current_endpoints.get(row['endpoint_id']) == row}
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
    return dict(status=status, error=relevant.get('error'), stale=stale,
                snapshot=snapshot, current_signature=current)
