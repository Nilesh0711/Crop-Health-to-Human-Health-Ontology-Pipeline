"""Offline neighboring-pair snapshots and comparisons; never create human gold."""
from collections import Counter
import json
from pathlib import Path

from backend.config.settings import APPLICATION_VERSION
from backend.shared.artifacts import save_json
from backend.shared.storage import file_hash, read_jsonl, stable_hash, utc_now


FORMAT = 'neighbor-benchmark-v1'
SCIENTIFIC_VERDICTS = {'connection_supported', 'components_supported', 'unresolved',
                       'contradicted', 'incompatible', 'out_of_scope'}


def _signature(value):
    return stable_hash(json.dumps(value, sort_keys=True, ensure_ascii=False), 64)


def snapshot_neighbors(run_directory, output):
    run_directory, output = Path(run_directory).resolve(), Path(output).resolve()
    root = run_directory.parent
    if not (run_directory / 'candidates.jsonl').is_file():
        raise ValueError('A saved document with candidates.jsonl is required')
    if output.is_relative_to(root):
        raise ValueError('Benchmark output must be outside the source ontology')
    if output.exists():
        raise ValueError('Benchmark output exists; choose a new filename')
    source = root / '.neighbor_validation/results.jsonl'
    files = [source, run_directory / 'summary.json', run_directory / 'candidates.jsonl',
             run_directory / 'source_validation.jsonl', run_directory / 'neighbor_evaluation_sample.jsonl',
             root / '.provisional_pathways/snapshot.json']
    before = {str(p): file_hash(p) for p in files if p.is_file()}
    saved = [r for r in read_jsonl(source) if run_directory.name in r['target']['document_keys']]
    if not saved:
        raise ValueError('No neighboring-pair records are available for this document')
    from backend.stages.neighbor_validation.records import read_records
    graph_path = root / '.provisional_pathways/snapshot.json'
    graph = json.loads(graph_path.read_text(encoding='utf-8')).get('graph') if graph_path.exists() else None
    scoped = [r for r in read_records(root, graph=graph) if run_directory.name in r['target']['document_keys']]
    records = [r for r in scoped if not r['stale']]
    historical = [r for r in scoped if r['stale']]
    summary_path = run_directory / 'summary.json'
    result = {'format': FORMAT, 'created_at': utc_now(), 'exporter_application_version': APPLICATION_VERSION,
              'document_key': run_directory.name, 'source_hashes': before, 'records': records,
              'historical_records': historical,
              'summary': json.loads(summary_path.read_text(encoding='utf-8')) if summary_path.exists() else {},
              'evaluation_sample': read_jsonl(run_directory / 'neighbor_evaluation_sample.jsonl')}
    if before != {str(p): file_hash(p) for p in files if p.is_file()}:
        raise ValueError('Source data changed during benchmark export; retry while idle')
    save_json(output, result)
    return {'output': str(output), 'pairs': len(records), 'historical_pairs': len(historical), 'human_labels_generated': False}


def _review_signature(record):
    return _signature({'target': record['target']['input_signature'], 'assessment': record.get('assessment'),
                       'paper_proofs': [p.get('proofs') for p in record.get('paper_assessments', [])]})


def _claim_signature(record):
    fields = ('subject', 'relation', 'object', 'claim_text', 'certainty', 'qualifiers',
              'source_evidence_span', 'source_pdf_sha256')
    return _signature([{'document': r['document_key'], 'candidate_id': r['claim']['candidate_id'],
                        'claim': {k: r['claim'].get(k) for k in fields}}
                       for r in record['target']['source_claims']])


def _metrics(snapshot):
    samples = {r['pathway_id']: r for r in snapshot.get('evaluation_sample', [])}
    reviewed, false_acceptances, missed_support, held_support = 0, 0, 0, 0
    calls = Counter()
    for record in snapshot['records']:
        calls.update({k: v for k, v in record.get('calls', {}).items() if type(v) is int})
        sample = samples.get(record['pathway_id'], {})
        gold = sample.get('gold') or {}
        if (not str(gold.get('reviewer') or '').strip() or gold.get('pathway_verdict') not in SCIENTIFIC_VERDICTS
                or sample.get('review_signature') != _review_signature(record)):
            continue
        reviewed += 1
        prediction, expected = record['verdict'], gold['pathway_verdict']
        false_acceptances += prediction == 'connection_supported' and expected != 'connection_supported'
        if expected == 'connection_supported' and prediction != expected:
            if prediction in SCIENTIFIC_VERDICTS and record.get('execution_status', 'complete') == 'complete':
                missed_support += 1
            else:
                held_support += 1
    summary = snapshot.get('summary') or {}
    return {'pairs': len(snapshot['records']),
            'historical_pairs_excluded': len(snapshot.get('historical_records', [])),
            'outcomes': dict(Counter(r['verdict'] for r in snapshot['records'])),
            'assessment_incomplete': sum(r['verdict'] == 'assessment_incomplete' for r in snapshot['records']),
            'per_pair_call_totals': dict(calls), 'wall_elapsed_seconds': summary.get('wall_elapsed_seconds'),
            'llm_usage': summary.get('llm_calls'), 'independently_reviewed': reviewed,
            'scientific_errors': {'false_acceptances': false_acceptances, 'missed_support': missed_support,
                                  'supported_gold_held_by_execution': held_support} if reviewed else None}


def compare_neighbors(before_path, after_path):
    snapshots = [json.loads(Path(p).read_text(encoding='utf-8')) for p in (before_path, after_path)]
    for snapshot in snapshots:
        if snapshot.get('format') != FORMAT or not isinstance(snapshot.get('records'), list):
            raise ValueError('Expected a neighbor-benchmark-v1 snapshot')
        ids = [r['pathway_id'] for r in snapshot['records']]
        if len(ids) != len(set(ids)):
            raise ValueError('Benchmark contains duplicate pathway identities')
    if snapshots[0]['document_key'] != snapshots[1]['document_key']:
        raise ValueError('Paired comparison requires the same source document')
    before, after = ({r['pathway_id']: r for r in s['records']} for s in snapshots)
    pairs = [{'pathway_id': identifier, 'before': before[identifier]['verdict'], 'after': after[identifier]['verdict'],
              'scientific_inputs_unchanged': _claim_signature(before[identifier]) == _claim_signature(after[identifier]),
              'mapping_changed': [(e['source'], e['target']) for e in before[identifier]['target']['edges']]
                                 != [(e['source'], e['target']) for e in after[identifier]['target']['edges']]}
             for identifier in sorted(before.keys() & after.keys())]
    return {'before': _metrics(snapshots[0]), 'after': _metrics(snapshots[1]), 'paired_targets': pairs,
            'removed_targets': sorted(before.keys() - after.keys()), 'new_targets': sorted(after.keys() - before.keys()),
            'automatic_gold_transfer': False,
            'limitations': ['Gold requires an independent reviewer and the exact saved assessment/evidence signature.',
                           'Changed source claims and removed/new targets are not identical paired assessments.',
                           'No scientific accuracy is established without reviewed gold; elapsed time and tokens are not monetary cost.']}


def compare_corpus(manifest_path):
    """Evaluate thematic documents separately; never double-count cross-PDF pairs."""
    manifest_path = Path(manifest_path).resolve()
    manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
    if not isinstance(manifest, dict) or not isinstance(manifest.get('comparisons'),list) or not manifest['comparisons']:
        raise ValueError('Corpus manifest requires a nonempty comparisons list')
    reports = []
    for item in manifest['comparisons']:
        if not isinstance(item,dict) or any(not isinstance(item.get(k),str) or not item[k].strip() for k in ('before','after')):
            raise ValueError('Each corpus comparison requires before and after snapshot paths')
        reports.append(compare_neighbors(manifest_path.parent/item['before'],manifest_path.parent/item['after']))
    return {'documents_compared':len(reports),'comparisons':reports,'global_accuracy_aggregated':False,
            'note':'Cross-document pairs may recur across snapshots. Independent evidence-bound gold is required per comparison; no accuracy is inferred from graph size.'}
