"""Index directed adjacency and assess each local two-claim connection once."""
import json
from copy import deepcopy
from collections import defaultdict
from threading import RLock
from backend.shared.storage import read_jsonl, stable_hash
from backend.stages.source_validation.scope import source_pathway_eligible

POLICY = 'neighbor-pair-v1'
ASSESSMENT_CONTRACT = 'qualified-local-evidence-v7'

_candidates_lock = RLock()
_candidates_cache = {}


def signature(value):
    return stable_hash(json.dumps(value, sort_keys=True, ensure_ascii=False), 64)


def source_signature(candidate):
    return signature({k: v for k, v in candidate.items() if k != 'semantic_validation'})


def load_candidates(root):
    with _candidates_lock:
        paths = sorted(root.glob('*/candidates.jsonl'))
        stamp = tuple((str(p), s.st_mtime_ns, s.st_size) for p in paths if (s := p.stat()))
        key = str(root.resolve())
        cached = _candidates_cache.get(key)
        if cached and cached['stamp'] == stamp:
            return cached['data']
        data = {(p.parent.name, c['candidate_id']): c for p in paths for c in read_jsonl(p)}
        _candidates_cache[key] = {'stamp': stamp, 'data': data}
        return data


def claim_signature(original):
    """Occurrence-bound, qualified scientific input; never a semantic cache hit."""
    return signature({'policy': POLICY, 'document': original['document_key'],
                      'source': source_signature(original['claim'])})


def make_target(chain, candidates):
    if len(chain) != 2 or chain[0]['target'] != chain[1]['source']:
        raise ValueError('A neighboring assessment requires exactly two directed adjacent edges')
    if chain[0]['id'] == chain[1]['id']:
        raise ValueError('A neighboring pair requires two distinct original claims')
    if chain[0].get('source') == chain[0].get('target') or chain[1].get('source') == chain[1].get('target'):
        raise ValueError('A neighboring pair cannot contain a self-loop edge')
    originals = []
    for edge in chain:
        candidate = candidates.get((edge['document_key'], edge['candidate_id']))
        if not candidate or not source_pathway_eligible(candidate):
            raise ValueError('A neighboring pair contains a missing, unsupported or out-of-scope source claim')
        originals.append({'edge_id': edge['id'], 'document_key': edge['document_key'],
                          'source_signature': source_signature(candidate), 'claim': deepcopy(candidate)})
    a, b = chain
    connection = {'connection_id': 'junction-' + signature([a['id'], b['id']])[:28],
                  'upstream_edge_id': a['id'], 'downstream_edge_id': b['id'],
                  'node_id': a['target'], 'label': a.get('target_label') or a['object']}
    target = {'pathway_id': 'neighbor-' + signature([a['id'], b['id']])[:28], 'policy': POLICY,
              'assessment_contract': ASSESSMENT_CONTRACT,
              'assessment_scope': 'neighboring_claims', 'whole_chain_assessment': 'not_assessed',
              'edges': chain, 'source_claims': originals, 'connections': [connection],
              'document_keys': sorted({e['document_key'] for e in chain}),
              'description': '\n'.join(f"Link {i}: " + (r['claim'].get('claim_text') or
                  f"{r['claim']['subject']} {r['claim']['relation']} {r['claim']['object']}") +
                  (f" [conditions: {json.dumps(r['claim']['qualifiers'], ensure_ascii=False)}]"
                   if r['claim'].get('qualifiers') else '') for i,r in enumerate(originals,1)),
              'causal_chain_validated': False}
    target['input_signature'] = signature({'policy': POLICY,
        'edges': [{k: e.get(k) for k in ('id', 'source', 'target', 'source_label', 'target_label')} for e in chain],
        'sources': [r['source_signature'] for r in originals], 'connection': connection})
    return target


class AdjacencyIndex:
    """No chain enumeration or similarity search is needed after identity alignment."""
    def __init__(self, edges):
        self.incoming, self.outgoing, self.edges = defaultdict(list), defaultdict(list), {}
        for edge in sorted(edges, key=lambda e: e['id']):
            self.edges[edge['id']] = edge
            if edge.get('source') != edge.get('target'):
                self.incoming[edge['target']].append(edge)
                self.outgoing[edge['source']].append(edge)

    def affected_pairs(self, edge_ids, *, preserve_order=False):
        seen = set()
        ordered = list(edge_ids) if preserve_order else sorted(edge_ids)
        for identifier in ordered:
            edge = self.edges[identifier]
            if edge.get('source') == edge.get('target'):
                continue
            for a, b in [*((a, edge) for a in self.incoming[edge['source']]),
                         *((edge, b) for b in self.outgoing[edge['target']])]:
                key = (a['id'], b['id'])
                if (key not in seen and a['id'] != b['id']
                        and a.get('source') != a.get('target') and b.get('source') != b.get('target')):
                    seen.add(key)
                    yield a, b


def enumerate_targets(graph, candidates, *, document_key=None, limit=100, edge_ids=None, completed_ids=()):
    index = AdjacencyIndex(graph.get('edges', []))
    if edge_ids is not None:
        relevant = sorted(
            (identifier for identifier in edge_ids if identifier in index.edges),
            key=lambda eid: (0 if document_key and index.edges[eid].get('document_key') == document_key else 1, eid)
        )
    else:
        relevant = [e['id'] for e in index.edges.values() if document_key is None or e['document_key'] == document_key]
    selected, has_more, involved, reused = [], False, set(), []
    for chain in index.affected_pairs(relevant, preserve_order=bool(document_key and edge_ids is not None)):
        identifier = 'neighbor-' + signature([e['id'] for e in chain])[:28]
        if identifier in completed_ids:
            reused.append(identifier)
            involved.update(e['id'] for e in chain)
            continue
        if len(selected) >= limit:
            has_more = True
            break
        selected.append(make_target(list(chain), candidates))
        involved.update(e['id'] for e in chain)
    return selected, {'selected_pairs': len(selected), 'selected_pathways': len(selected),
        'assessment_scope': 'neighboring_claims', 'whole_chain_assessment': 'not_assessed',
        'has_more': has_more, 'depth_limited': False, 'max_hops': 2,
        'reused_pair_ids': reused,
        'pair_budget': limit, 'complete_pair_coverage': not has_more,
        'complete_pathway_coverage': False,
        'claims_outside_selected_pathways': [identifier for identifier in relevant if identifier not in involved],
        'standalone_claims_are_externally_unvalidated': True}


def search_targets(target):
    rows = []
    for original in target['source_claims']:
        c = original['claim']
        conditions = '; '.join(f'{k}: {v}' for k, v in (c.get('qualifiers') or {}).items() if v)
        claim_text=(c.get('claim_text') or '').strip() or f"{c['subject']} {c['relation']} {c['object']}"
        rows.append({'target_id': original['edge_id'], 'kind': 'link',
            'text': claim_text + ('; ' + conditions if conditions else ''),
            'neutral_text': ' '.join([c['subject'], c['object'], conditions]),
            'edge_ids': [original['edge_id']], **{k: c.get(k) for k in
                ('subject', 'object', 'relation', 'claim_text', 'certainty', 'qualifiers', 'subject_type', 'object_type')}})
    a, b = target['edges']
    rows.append({'target_id': target['connections'][0]['connection_id'], 'kind': 'connection',
        'text': target['description'] + '. Compare the shared entity and locally applicable conditions.',
        'neutral_text': ' '.join([a['subject'], a['object'], b['object']]),
        'edge_ids': [a['id'], b['id']]})
    return rows


def current_target(target, root, graph=None, candidates=None):
    if (target.get('policy') != POLICY or target.get('assessment_contract') != ASSESSMENT_CONTRACT
            or len(target.get('edges', [])) != 2):
        return False
    candidates = load_candidates(root) if candidates is None else candidates
    try:
        current = make_target(target['edges'], candidates)
    except (KeyError, ValueError):
        return False
    if current['pathway_id'] != target.get('pathway_id') or current['input_signature'] != target.get('input_signature'):
        return False
    if graph is not None:
        edges = {e['id']: e for e in graph.get('edges', [])}
        return all(e['id'] in edges and all(e.get(k) == edges[e['id']].get(k)
            for k in ('source', 'target', 'source_label', 'target_label')) for e in target['edges'])
    return True
