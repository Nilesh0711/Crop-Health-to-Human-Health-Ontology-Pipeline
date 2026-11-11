"""Independent mapping/identity labels stay separate from model predictions."""
import json
from itertools import combinations

from backend.config.pipeline import ONTOLOGY_WORKSPACE_DIRECTORY as DIRECTORY
from backend.shared.artifacts import save_rows
from backend.shared.storage import read_jsonl


def export_sample(root, destination):
    if destination.exists():
        raise ValueError('Benchmark file already exists; choose a new file to preserve reviewer labels')
    path = root / DIRECTORY / 'snapshot.json'
    state = json.loads(path.read_text(encoding='utf-8'))
    rows = []
    for assertion in state['assertions']:
        for role in ('subject', 'object'):
            binding = assertion['bindings'][role]
            rows.append({'id': binding['occurrence_id'], 'document': assertion['document_key'],
                'claim_id': assertion['candidate_id'], 'role': role,
                'source': assertion['original_claim'], 'catalog_signature': state['catalog_signature'],
                'claim_signature': assertion['claim_signature'],
                'prediction': binding, 'predicted_scope': assertion['scope'],
                'gold': {'reviewed': False, 'ontology_iri': None, 'identity_group': None,
                         'scope': None, 'reviewer': None, 'notes': None}})
    save_rows(destination, rows)
    return {'rows': len(rows), 'labelled': 0, 'accuracy': None}


def evaluate(path, *, root=None):
    rows = read_jsonl(path)
    if root is not None:
        # Match frozen labels only to unchanged source/reference inputs. Never
        # copy labels onto a different extraction, scope or reference version.
        saved = {(p.parent.name, r['candidate_id']): r
                 for p in root.glob('*/ontology_grounding.jsonl') for r in read_jsonl(p)}
        for row in rows:
            current = saved.get((row['document'], row['claim_id']))
            compatible = current and all(current.get(k) == row.get(k) for k in ('catalog_signature', 'claim_signature'))
            row['prediction'] = current['bindings'][row['role']] if compatible else None
            row['predicted_scope'] = current['scope'] if compatible else None
    labels = [r for r in rows if r['gold'].get('reviewed') and r['gold'].get('reviewer')]
    reviewed = [r for r in labels if isinstance(r.get('prediction'), dict)]
    def ratio(n, d):
        return n / d if d else None
    exact = sum(r['prediction'].get('iri') == r['gold']['ontology_iri'] for r in reviewed)
    mapped_gold = [r for r in reviewed if r['gold']['ontology_iri']]
    recall = sum(r['gold']['ontology_iri'] in r['prediction'].get('candidate_iris', []) for r in mapped_gold)
    labelled_identity = [r for r in reviewed if r['gold'].get('identity_group') is not None]
    correct_merges = wrong_merges = missed = 0
    for left, right in combinations(labelled_identity, 2):
        predicted = left['prediction']['entity_id'] == right['prediction']['entity_id']
        gold = left['gold']['identity_group'] == right['gold']['identity_group']
        correct_merges += predicted and gold
        wrong_merges += predicted and not gold
        missed += gold and not predicted
    scope_rows = [r for r in reviewed if r['gold'].get('scope')]
    alignment_rows = [r for r in reviewed if r['gold'].get('mapping')]
    denotation_rows = [r for r in reviewed if r['gold'].get('denotation')]
    return {'rows': len(rows), 'reviewed': len(reviewed), 'mapping_accuracy': ratio(exact, len(reviewed)),
            'reviewed_labels': len(labels), 'labels_without_compatible_predictions': len(labels) - len(reviewed),
            'alignment_accuracy': ratio(sum(r['prediction'].get('mapping') == r['gold']['mapping']
                and r['prediction'].get('iri') == r['gold']['ontology_iri'] for r in alignment_rows), len(alignment_rows)),
            'denotation_accuracy': ratio(sum(r['prediction'].get('denotation') == r['gold']['denotation']
                                            for r in denotation_rows), len(denotation_rows)),
            'candidate_recall': ratio(recall, len(mapped_gold)), 'incorrect_merges': wrong_merges,
            'missed_equivalents': missed,
            'identity_precision': ratio(correct_merges, correct_merges + wrong_merges),
            'identity_recall': ratio(correct_merges, correct_merges + missed),
            'scope_accuracy': ratio(sum(r['predicted_scope'] == r['gold']['scope'] for r in scope_rows), len(scope_rows)),
            'scientific_connection_accuracy': None,
            'note': 'Identity metrics do not measure scientific connection validity. Unlabelled examples are not counted as correct.'}
