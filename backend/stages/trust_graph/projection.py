"""Reuse provisional identity alignment when publishing validated graph membership."""
from backend.shared.artifacts import save_json
from backend.shared.storage import utc_now
from backend.stages.pathway_assembly.corpus import (
    collect_corpus, corpus_signature, ASSEMBLY_VERSION, ASSEMBLY_ELIGIBILITY_POLICY,
)
from backend.stages.trust_graph.ledger import decorate_graph


def publish_graphs(root, provisional, settings):
    for name in ('.provisional_pathways', '.trust_graph', '.pathways'):
        (root / name).mkdir(parents=True, exist_ok=True)
    raw_edges, endpoints = collect_corpus(root, provisional=True)
    old = {e['id']: e for e in provisional['graph']['edges']}
    if set(old) != {e['id'] for e in raw_edges}:
        raise ValueError('Source-grounded corpus changed during trust assessment; reassemble provisional identities')
    provisional = {**provisional, 'endpoints': endpoints,
        'input_signature': corpus_signature(raw_edges, endpoints, settings.pathway_assembly_model,
                                             settings.task_thinking['pathway_assembly'], provisional=True),
        'graph': {**provisional['graph'], 'edges': [{**old[e['id']], **e} for e in raw_edges]},
        'completed_at': utc_now()}
    save_json(root / '.provisional_pathways' / 'snapshot.json', provisional)
    trusted = {**provisional, 'graph': decorate_graph(root, provisional['graph']), 'trust_policy': 'distinct-study-evidence-balance-v1'}
    save_json(root / '.trust_graph' / 'snapshot.json', trusted)
    valid, valid_endpoints = collect_corpus(root)
    valid_ids = {e['id'] for e in valid}
    kept = [{**old[e['id']], **e, 'provisional': False} for e in valid]
    kept_nodes = {e[role] for e in kept for role in ('source', 'target')}
    nodes = [{**n, 'endpoint_ids': [eid for eid in n['endpoint_ids'] if eid.rsplit(':', 1)[0] in valid_ids]}
             for n in provisional['graph']['nodes'] if n['id'] in kept_nodes]
    signature = corpus_signature(valid, valid_endpoints, settings.pathway_assembly_model,
                                  settings.task_thinking['pathway_assembly'])
    validated = {**provisional, 'version': ASSEMBLY_VERSION, 'input_signature': signature,
        'eligibility_policy': ASSEMBLY_ELIGIBILITY_POLICY, 'alignment_origin': 'provisional_projection',
        'graph': {'nodes': nodes, 'edges': kept, 'counts': {'nodes': len(nodes), 'edges': len(kept)}},
        'endpoints': valid_endpoints, 'counts': {'claims': len(kept), 'endpoints': 2 * len(kept),
            'entities': len(nodes), 'papers': len({e['document_key'] for e in kept})}}
    save_json(root / '.pathways' / 'snapshot.json', validated)
    save_json(root / '.pathways' / 'checkpoint.json', {'input_signature': signature, 'status': 'completed',
                                                      'updated_at': utc_now(), 'alignment_origin': 'provisional_projection'})
    return validated
