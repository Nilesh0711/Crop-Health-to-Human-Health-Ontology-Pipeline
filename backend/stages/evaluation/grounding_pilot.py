"""Small source-bound review sets; candidate retrieval never supplies gold labels."""
from collections import defaultdict, deque

from backend.ontology.signatures import claim_signature
from backend.shared.artifacts import save_rows
from backend.shared.storage import read_jsonl, stable_hash
from backend.stages.ontology_grounding.retrieval import ConceptIndex
from backend.stages.ontology_grounding.grounder import ground_claims


def prepare_pilot(root, reference, destination, *, document=None, limit=20):
    if not 1 <= limit <= 50:
        raise ValueError('A small grounding pilot must contain 1–50 mentions')
    if destination.exists():
        raise ValueError('Pilot file already exists; preserve reviewer labels and choose a new file')
    index = ConceptIndex(reference)
    buckets, seen = defaultdict(deque), set()
    focus = set(reference.profile.get('focus_classes', []))
    for path in sorted(root.glob('*/candidates.jsonl')):
        if document and path.parent.name != document:
            continue
        for claim in read_jsonl(path):
            if claim.get('schema_error') or claim.get('superseded_by'):
                continue
            for role in ('subject', 'object'):
                expression = claim[role]
                mention = claim.get(role + '_source_mention') or expression
                # Keep repeated words in different study contexts; don't pad with
                # identical occurrences in duplicated extracted claims.
                context = claim.get('source_local_context') or claim.get('source_evidence_span')
                key = (path.parent.name, expression.casefold(), mention.casefold(), context)
                if key in seen:
                    continue
                seen.add(key)
                hits = index.retrieve(expression, mention, context or '')
                best = hits[0]['iri'] if hits else 'unmapped'
                bucket = (0 if best in focus else 1, best)
                occurrence = 'mention-' + stable_hash(f'{path.parent.name}|{claim["candidate_id"]}|{role}', 32)
                buckets[bucket].append({'id': occurrence, 'document': path.parent.name,
                    'claim_id': claim['candidate_id'], 'role': role, 'source': claim,
                    'catalog_signature': reference.signature, 'claim_signature': claim_signature(claim),
                    'candidate_terms': [{'iri': h['iri'], 'label': h['label'], 'definition': h['definition'],
                                         'retrieval_signals': h['retrieval_signals']} for h in hits],
                    'prediction': None,
                    'gold': {'reviewed': False, 'ontology_iri': None, 'mapping': None, 'denotation': None,
                             'identity_group': None, 'scope': None, 'reviewer': None, 'notes': None}})
    selected = []
    # Round-robin across reference candidates includes clear hits and vocabulary
    # gaps. This samples review cases, not scientific relevance or correctness.
    while len(selected) < limit and any(buckets.values()):
        for bucket in sorted(buckets):
            if buckets[bucket] and len(selected) < limit:
                selected.append(buckets[bucket].popleft())
    if not selected:
        raise ValueError('No extracted mentions found for the selected document')
    save_rows(destination, selected)
    return {'rows': len(selected), 'labelled': 0, 'model_calls': 0,
            'catalog_signature': reference.signature, 'note': 'Candidate terms are suggestions, not reviewed answers.'}


def run_pilot(sample, reference, destination, client, *, encoder=None, emit=lambda *args: None):
    """Assess frozen source claims in an isolated directory; never send gold labels."""
    from pathlib import Path
    grouped = defaultdict(dict)
    for row in sample:
        document = row['document']
        if Path(document).name != document or document in {'.', '..'}:
            raise ValueError('Pilot document must be a saved folder name')
        if row['catalog_signature'] != reference.signature:
            raise ValueError('Pilot reference changed; prepare a new review set instead of transferring labels')
        claim = row['source']
        if row['claim_signature'] != claim_signature(claim) or row['claim_id'] != claim['candidate_id']:
            raise ValueError('Pilot claim changed; do not reuse its labels')
        grouped[document][row['claim_id']] = claim
    if not grouped:
        raise ValueError('Pilot review set is empty')
    if destination.exists() and any(destination.iterdir()):
        raise ValueError('Pilot result directory is not empty; choose a new directory to preserve comparisons')
    counts = {'review_mentions': len(sample), 'source_claims': 0, 'grounded_endpoint_mentions': 0}
    for document, claims in grouped.items():
        output = destination / document
        output.mkdir(parents=True, exist_ok=True)
        save_rows(output / 'candidates.jsonl', list(claims.values()))
        results = ground_claims(list(claims.values()), output, reference, client, encoder=encoder, emit=emit)
        counts['source_claims'] += len(results)
        counts['grounded_endpoint_mentions'] += sum(b['mapping'] == 'exact' for r in results
            for role, b in r['bindings'].items() if role != 'relation')
    return counts
