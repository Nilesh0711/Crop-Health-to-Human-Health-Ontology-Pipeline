"""Publish local connection findings while retaining read-only historical whole routes."""
import json
from copy import deepcopy
from threading import RLock
from collections import Counter
from backend.shared.artifacts import save_rows, save_json, _path_lock, _publish
from backend.shared.storage import read_jsonl, utc_now
from backend.stages.neighbor_validation.targets import ASSESSMENT_CONTRACT, POLICY, current_target, load_candidates, signature

DIRECTORY = '.neighbor_validation'

_record_lock = RLock()
_raw_cache = {}
_eval_cache = {}


def _load_raw_records(results_path):
    with _record_lock:
        if not results_path.exists():
            return {}, {}
        stat = results_path.stat()
        key = str(results_path.resolve())
        cached = _raw_cache.get(key)
        if cached and cached['mtime_ns'] == stat.st_mtime_ns and cached['size'] == stat.st_size:
            return cached['rows'], cached['lines']
        rows, lines = {}, {}
        with results_path.open('r', encoding='utf-8') as f:
            for raw_line in f:
                s = raw_line.strip()
                if not s:
                    continue
                obj = json.loads(s)
                pid = obj['pathway_id']
                rows[pid] = obj
                lines[pid] = s
        _raw_cache[key] = {'mtime_ns': stat.st_mtime_ns, 'size': stat.st_size, 'rows': rows, 'lines': lines}
        return rows, lines


def read_records(root, *, graph=None):
    rows_map, _ = _load_raw_records(root / DIRECTORY / 'results.jsonl')
    records = list(rows_map.values())
    candidates = load_candidates(root)
    cands_id = id(candidates)
    edges = {e['id']: e for e in graph.get('edges', [])} if graph is not None else None
    from backend.stages.neighbor_validation.proof_audit import audit_signature
    result = []
    for row in records:
        target = row['target']
        cache_key = (row['pathway_id'], row.get('updated_at'), row.get('validation_signature'),
                     row.get('verdict'), target.get('input_signature'), cands_id)
        cached_flags = _eval_cache.get(cache_key)
        if cached_flags is None:
            base_valid = current_target(target, root, None, candidates)
            audit = row.get('proof_audit') or {}
            audited = audit.get('passed') is True and audit.get('input_signature') == audit_signature(
                target, row.get('paper_assessments', []), row.get('assessment'))
            cached_flags = (base_valid, audited)
            _eval_cache[cache_key] = cached_flags
        base_valid, audited = cached_flags
        if base_valid and edges is not None:
            valid = all(e['id'] in edges and all(e.get(k) == edges[e['id']].get(k)
                for k in ('source', 'target', 'source_label', 'target_label')) for e in target['edges'])
        else:
            valid = base_valid
        result.append({**row, 'stale': not valid, 'eligible': valid and row.get('verdict') == 'connection_supported' and audited})
    return result


def save_record(root, record):
    save_records(root, [record])


def save_records(root, records):
    results_path = root / DIRECTORY / 'results.jsonl'
    with _record_lock:
        rows, lines = _load_raw_records(results_path)
        rows = dict(rows)
        lines = dict(lines)
        history = None
        for record in records:
            pid = record['pathway_id']
            previous = rows.get(pid)
            if previous and (previous.get('assessment_id') != record.get('assessment_id') or
                             previous.get('validation_signature') != record.get('validation_signature')):
                if history is None:
                    history = read_jsonl(root / DIRECTORY / 'assessment_history.jsonl')
                history.append({'archived_at':utc_now(),'replaced_by_assessment_id':record.get('assessment_id'),
                                'record':previous})
            rows[pid] = deepcopy(record)
            lines[pid] = json.dumps(record, ensure_ascii=False)
        if history is not None:
            save_rows(root / DIRECTORY / 'assessment_history.jsonl', history)
        with _path_lock(results_path):
            results_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = results_path.with_name(results_path.name + '.tmp')
            temporary.write_text((''.join(line + '\n' for line in lines.values())), encoding='utf-8')
            _publish(temporary, results_path)
            stat = results_path.stat()
            _raw_cache[str(results_path.resolve())] = {
                'mtime_ns': stat.st_mtime_ns, 'size': stat.st_size, 'rows': rows, 'lines': lines
            }


def historical_records(root, graph=None, document_key=None):
    """Read old outcomes without importing or executing archived scientific logic."""
    from backend.stages.neighbor_validation.targets import source_signature
    from backend.stages.source_validation.scope import source_pathway_eligible
    candidates = load_candidates(root)
    edges = {e['id']:e for e in (graph or {}).get('edges',[])}
    rows = []
    for row in read_jsonl(root / '.pathway_validation' / 'results.jsonl'):
        target = row.get('target',{})
        if document_key and document_key not in target.get('document_keys',[]):
            continue
        valid = all(source_pathway_eligible(candidates.get((r['document_key'],r['claim']['candidate_id']),{}))
            and source_signature(candidates[(r['document_key'],r['claim']['candidate_id'])]) == r.get('source_signature')
            for r in target.get('source_claims',[]))
        if graph is not None:
            valid = valid and all(e['id'] in edges and all(e.get(k)==edges[e['id']].get(k)
                for k in ('source','target','source_label','target_label')) for e in target.get('edges',[]))
        rows.append({**row,'historical':True,'assessment_scope':'whole_pathway',
                     'stale':not valid,'eligible':False,
                     'historical_eligible':valid and row.get('verdict')=='pathway_supported'})
    return rows


def assessment_view(root, document_key=None):
    from backend.stages.pathway_assembly.assembly import read_assembly
    from backend.config.settings import Settings
    assembly = read_assembly(root, Settings().pathway_assembly_model, provisional=True)
    graph = (assembly.get('snapshot') or {}).get('graph', {'nodes': [], 'edges': []})
    rows = read_records(root, graph=graph)
    if document_key:
        rows = [r for r in rows if document_key in r['target']['document_keys']]
    coverage_path = root / document_key / 'pathway_coverage.json' if document_key else root / DIRECTORY / 'coverage.json'
    coverage = json.loads(coverage_path.read_text(encoding='utf-8')) if coverage_path.exists() else {}
    return {'policy': 'neighbor-pair-v1', 'assessment_contract':ASSESSMENT_CONTRACT,
            'records': rows, 'counts': dict(Counter('stale' if r['stale'] else r['verdict'] for r in rows)),
            'coverage': coverage, 'historical_records': historical_records(root, graph, document_key),
            'assessment_scope':'neighboring_claims', 'whole_chain_assessment':'not_assessed', 'standalone_claims_are_externally_unvalidated': True,
            'assembly_stale': assembly.get('stale', False)}


def validated_graph(root, provisional, *, rows=None):
    rows = read_records(root, graph=provisional['graph']) if rows is None else rows
    qualified = [r for r in rows if r['eligible']]
    memberships = {}
    for row in qualified:
        for edge in row['target']['edges']:
            memberships.setdefault(edge['id'], []).append(row['pathway_id'])
    edges = [{**e, 'status': 'local_connection_supported', 'external_status': 'local_connection_membership',
              'supported_pathway_ids': memberships[e['id']], 'supported_connection_ids':memberships[e['id']], 'individually_externally_validated': False,
              'causal_chain_validated': False} for e in provisional['graph']['edges'] if e['id'] in memberships]
    node_ids = {e[k] for e in edges for k in ('source','target')}
    nodes = [n for n in provisional['graph']['nodes'] if n['id'] in node_ids]
    return {'nodes': nodes, 'edges': edges, 'counts': {'nodes': len(nodes), 'edges': len(edges),
            'supported_connections': len(qualified), 'supported_pathways':0, 'supported': 0, 'corroborated': 0},
            'local_connection_membership_only':True, 'whole_chain_assessment':'not_assessed', 'pathway_membership_only': True, 'graph_union_is_not_a_validated_pathway': True}


def publish_views(root, provisional):
    (root / DIRECTORY).mkdir(parents=True, exist_ok=True)
    all_rows = read_records(root, graph=provisional['graph'])
    graph = validated_graph(root, provisional, rows=all_rows)
    save_json(root / DIRECTORY / 'graph.json', graph)
    by_document = {e['document_key'] for e in provisional['graph']['edges']}
    for folder in root.iterdir():
        if folder.is_dir() and not folder.name.startswith('.') and (folder / 'candidates.jsonl').exists():
            by_document.add(folder.name)
    for document in by_document:
        from backend.stages.graph_construction.graph import graph_from_candidates, write_graphml
        folder = root / document
        # Preserve the prior published artifacts before converting their active
        # display to pathway membership. Existing validation ledgers stay intact.
        archive = root / DIRECTORY / 'historical_views' / document
        if not archive.exists():
            import shutil
            archive.mkdir(parents=True)
            for name in ('graph.json','source_graph.json','audit_graph.json','knowledge_graph.graphml','reasoning_paths.json','summary.json'):
                if (folder / name).is_file():
                    shutil.copy2(folder / name,archive / name)
        candidates = read_jsonl(folder / 'candidates.jsonl')
        local = [{**e, 'id': e['candidate_id']} for e in graph['edges'] if e['document_key'] == document]
        ids = {e[k] for e in local for k in ('source','target')}
        local_graph = {**graph, 'nodes': [n for n in graph['nodes'] if n['id'] in ids], 'edges': local,
                       'counts': {**graph['counts'], 'nodes': len(ids), 'edges': len(local),
                                  'supported_connections':len({p for e in local for p in e['supported_pathway_ids']}), 'supported_pathways':0}}
        save_json(folder / 'graph.json', local_graph)
        for view, filename in [('source','source_graph.json'),('audit','audit_graph.json')]:
            saved = graph_from_candidates(candidates, [], view=view)
            for edge in saved['edges']:
                edge.pop('trust', None)
                edge['individually_externally_validated'] = False
            save_json(folder / filename, saved)
        write_graphml(local_graph, folder / 'knowledge_graph.graphml')
        save_json(folder / 'reasoning_paths.json', [])
        save_json(folder / 'external_validation_target.json',{'target':'neighboring_claims','policy':'neighbor-pair-v1','updated_at':utc_now()})
        doc_rows = [r for r in all_rows if document in r['target']['document_keys']]
        doc_counts = dict(Counter('stale' if r['stale'] else r['verdict'] for r in doc_rows))
        coverage_path = folder / 'pathway_coverage.json'
        doc_coverage = json.loads(coverage_path.read_text(encoding='utf-8')) if coverage_path.exists() else {}
        summary_path = folder / 'summary.json'
        summary_data = json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else {}
        from backend.stages.source_validation.summary import source_assessment_counts
        summary_data.update(
            knowledge_building_mode='pathway_only',
            external_validation_target='neighboring_claims',
            extracted=len(candidates),
            schema_valid=sum(not c.get('schema_error') for c in candidates),
            **source_assessment_counts(candidates),
            externally_evaluated=0,
            external_incomplete=0,
            pathway_validation={'outcomes': doc_counts, 'coverage': doc_coverage},
            graph=local_graph['counts'],
            updated_at=utc_now()
        )
        save_json(summary_path, summary_data)
        neighbor_summary = {
            'policy': POLICY, 'assessment_scope': 'neighboring_claims',
            'whole_chain_assessment': 'not_assessed',
            'assessment_contract': ASSESSMENT_CONTRACT,
            'assessed_pairs': sum(v for k, v in doc_counts.items() if k not in (None, 'queued')),
            'outcomes': doc_counts, 'coverage': doc_coverage,
            'graph': local_graph['counts'], 'updated_at': utc_now()
        }
        save_json(folder / 'neighbor_validation_summary.json', neighbor_summary)
    return graph


def validated_paths(root, *, limit=100, max_hops=6, offset=0):
    data = assessment_view(root)
    if not (root / DIRECTORY).exists():
        return historical_paths(root, data, limit, max_hops, offset)
    rows = [r for r in data['records'] if r['eligible'] and len(r['target']['edges']) <= max_hops]
    rows.sort(key=lambda r: r['pathway_id'])
    page = rows[offset:offset+limit]
    graph_path = root / DIRECTORY / 'graph.json'
    graph = json.loads(graph_path.read_text(encoding='utf-8')) if graph_path.exists() else {'nodes': [], 'edges': []}
    # Never enumerate the union: that would fabricate unassessed combinations.
    valid_ids = {e['id'] for r in rows for e in r['target']['edges']}
    edges = [e for e in graph.get('edges', []) if e['id'] in valid_ids]
    nodes = {e[k] for e in edges for k in ('source','target')}
    return {'policy': 'neighbor-pair-v1', 'view': 'validated', 'status': 'completed',
            'graph': {**graph, 'edges': edges, 'nodes': [n for n in graph.get('nodes',[]) if n['id'] in nodes]},
            'paths': [r['target']['edges'] for r in page], 'pathway_assessments': page,
            'counts': {'claims': len(edges), 'entities': len(nodes), 'papers': len({e['document_key'] for e in edges})},
            'has_more': offset+limit < len(rows), 'offset': offset, 'limit': limit, 'max_hops': max_hops,
            'assessment_scope':'neighboring_claims', 'whole_chain_assessment':'not_assessed',
            'causal_chain_validated': False, 'score_is_probability': False, 'stale': data['assembly_stale']}


def write_evaluation_sample(root, output):
    """Keep model predictions separate from independently entered human labels."""
    path = output / 'neighbor_evaluation_sample.jsonl'
    old = {r['pathway_id']:r for r in read_jsonl(path)}
    history_path = output / 'neighbor_evaluation_history.jsonl'
    history = read_jsonl(history_path)
    samples = []
    for row in read_records(root):
        if output.name not in row['target']['document_keys']:
            continue
        evidence = signature({'target':row['target']['input_signature'],'assessment':row.get('assessment'),
                              'paper_proofs':[p.get('proofs') for p in row.get('paper_assessments',[])]})
        previous = old.get(row['pathway_id'],{})
        if previous and previous.get('review_signature') != evidence and any(
                value is not None for value in previous.get('gold',{}).values()) and not any(
                h['pathway_id']==previous['pathway_id'] and h['review_signature']==previous['review_signature'] for h in history):
            history.append(previous)
        gold = previous.get('gold') if previous.get('review_signature') == evidence else None
        samples.append({'pathway_id':row['pathway_id'],'description':row['target']['description'],
            'assessment_contract':row.get('assessment_contract') or row['target'].get('assessment_contract'),
            'review_signature':evidence,'prediction':{'verdict':row['verdict'],'basis':row.get('evidence_basis'),'stale':row['stale']},
            'gold':gold or {'pathway_verdict':None,'connection_compatibility':None,'proof_correct':None,'reviewer':None,'notes':None}})
    save_rows(path,samples)
    if history:
        save_rows(history_path,history)


def historical_paths(root, data, limit=100, max_hops=6, offset=0):
    rows = [r for r in data.get('historical_records',[]) if r.get('historical_eligible') and len(r['target']['edges'])<=max_hops]
    rows.sort(key=lambda r:r['pathway_id'])
    page = rows[offset:offset+limit]
    edges = list({e['id']:e for r in rows for e in r['target']['edges']}.values())
    nodes = {e[k] for e in edges for k in ('source','target')}
    labels = {e[k]:e.get(k+'_label') or e.get('subject' if k=='source' else 'object',e[k]) for e in edges for k in ('source','target')}
    return {'policy':'whole-pathway-v1','view':'validated','status':'historical','historical':True,
        'graph':{'edges':edges,'nodes':[{'id':i,'label':labels[i]} for i in sorted(nodes)]},
        'paths':[r['target']['edges'] for r in page],'pathway_assessments':page,
        'counts':{'claims':len(edges),'entities':len(nodes),'papers':len({e['document_key'] for e in edges})},
        'has_more':offset+limit<len(rows),'offset':offset,'limit':limit,'max_hops':max_hops,
        'causal_chain_validated':False,'stale':data.get('assembly_stale',False)}
