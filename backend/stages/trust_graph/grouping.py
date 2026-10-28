"""Bounded graph-neighbour batches; disconnected edges stay in the work queue."""
from collections import defaultdict


def claim_batches(candidates, graph, document_key, size):
    remaining = {c['candidate_id']: c for c in candidates}
    local_edges = {e['candidate_id']: e for e in graph.get('edges', []) if e['document_key'] == document_key}
    nodes = defaultdict(set)
    for cid, edge in local_edges.items():
        for endpoint in ('source', 'target'):
            nodes[edge[endpoint]].add(cid)
    while remaining:
        seed = next(iter(remaining))
        queue, chosen = [seed], []
        while queue and len(chosen) < size:
            cid = queue.pop(0)
            if cid not in remaining:
                continue
            chosen.append(remaining.pop(cid))
            edge = local_edges.get(cid)
            if edge:
                queue.extend(sorted((nodes[edge['source']] | nodes[edge['target']]) & remaining.keys()))
        if len(chosen) < size:
            # Disconnected claims get equal assessment, not silent exclusion.
            for cid in list(remaining)[:size-len(chosen)]:
                chosen.append(remaining.pop(cid))
        yield chosen


def connections(candidates, graph, document_key):
    ids = {c['candidate_id'] for c in candidates}
    rows = [e for e in graph.get('edges', []) if e['document_key'] == document_key and e['candidate_id'] in ids]
    return [{'upstream_claim_id': a['candidate_id'], 'downstream_claim_id': b['candidate_id'],
             'shared_entity': a.get('target_label', a['target'])}
            for a in rows for b in rows if a['id'] != b['id'] and a['target'] == b['source']]
