"""Enumerate directed chains of original edges, without causal-chain validation."""
from collections import defaultdict


def assembled_paths(graph, limit=100, max_hops=6, offset=0):
    outgoing, incoming = defaultdict(list), set()
    for edge in graph.get('edges', []):
        outgoing[edge['source']].append(edge)
        incoming.add(edge['target'])
    roots = sorted(set(outgoing) - incoming)
    # Also visit cyclic/disconnected components with no zero-indegree root.
    starts = roots + sorted(set(outgoing) - set(roots))
    seen, paths = set(), []
    stop = offset + limit + 1
    def visit(node, visited, path):
        if len(paths) >= stop:
            return
        next_edges = [e for e in outgoing[node] if e['target'] not in visited]
        if path and (not next_edges or len(path) >= max_hops):
            key = tuple(e['id'] for e in path)
            if len(path) >= 2 and key not in seen:
                seen.add(key)
                paths.append(path)
            return
        for edge in next_edges:
            visit(edge['target'], visited | {edge['target']}, [*path, edge])
            if len(paths) >= stop:
                break
    for node in starts:
        visit(node, {node}, [])
        if len(paths) >= stop:
            break
    return dict(paths=paths[offset:offset + limit], has_more=len(paths) > offset + limit,
                offset=offset, limit=limit, max_hops=max_hops, causal_chain_validated=False)
