"""Stable same-evidence groups and non-destructive interpretation warnings."""
import json
from collections import defaultdict

from backend.shared.storage import stable_hash
from backend.ontology.signatures import claim_signature
from backend.domain.extraction_review import refresh_eligibility

GROUPING_POLICY = 'same-evidence-groups-v1'


def evidence_groups(candidates, maximum):
    buckets = {}
    for claim in candidates:
        numbers = claim.get('source_evidence_sentence_numbers') or []
        key = (claim.get('source_document_id') or claim.get('source_paper_title'),
               claim.get('source_packet_number'), tuple(sorted(numbers)),
               claim.get('source_evidence_span') if numbers else claim['candidate_id'])
        buckets.setdefault(key, []).append(claim)
    groups = []
    for claims in buckets.values():
        claims = sorted(claims, key=lambda c: c['candidate_id'])
        groups.extend(claims[i:i + maximum] for i in range(0, len(claims), maximum))
    return groups


def group_signature(claims):
    return stable_hash(json.dumps([(c['candidate_id'], claim_signature(c)) for c in claims]), 64)


def flag_consistency(rows):
    groups = defaultdict(list)
    for row in rows:
        row['mapping_consistency_warnings'] = []
        claim = row['original_claim']
        for role in ('subject', 'object', 'relation'):
            key = (row.get('group_signature'), role, str(claim[role]).casefold().strip(),
                   str(claim.get(role + '_context') or '').casefold().strip(),
                   json.dumps(claim.get('qualifiers') or {}, sort_keys=True))
            groups[key].append(row)
    for key, siblings in groups.items():
        role = key[1]
        decisions = {(r['bindings'][role].get('iri'), r['bindings'][role]['mapping'],
                      r['bindings'][role].get('denotation')) for r in siblings}
        if len(decisions) > 1:
            for row in siblings:
                row['mapping_consistency_warnings'].append({'role': role,
                    'message': 'Identical contextual input received conflicting mappings; decisions are preserved for review.',
                    'candidate_ids': [r['candidate_id'] for r in siblings]})
    return [refresh_eligibility(row) for row in rows]
