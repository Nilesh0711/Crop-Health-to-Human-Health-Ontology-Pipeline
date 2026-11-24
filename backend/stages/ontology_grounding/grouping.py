"""Stable same-evidence groups and non-destructive interpretation warnings."""
import json
from collections import defaultdict

from backend.shared.storage import stable_hash
from backend.ontology.signatures import claim_signature
from backend.domain.extraction_review import refresh_eligibility, review_status
from backend.ontology.policies import ENTITY_GROUNDING_POLICY

GROUPING_POLICY = 'same-evidence-groups-v1'
PREVIOUS_CONSISTENCY_POLICY = 'document-context-interpretation-v2'
CONSISTENCY_POLICY = 'document-context-interpretation-v3'


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


def _wording(value):
    return ' '.join(str(value or '').split()).casefold()


def endpoint_identity(row, role):
    """Retain wording AND alignment; a shared broader class is not identity."""
    claim, binding = row['original_claim'], row['bindings'][role]
    return (_wording(claim[role]), _wording(claim.get(role + '_context')),
            binding.get('canonical_iri') or binding.get('iri'),
            binding.get('mapping'), binding.get('denotation'))


def _context_key(row):
    claim = row['original_claim']
    evidence = claim.get('source_evidence_span')
    # No evidence means no safe cross-record comparison.
    return (row.get('document_key'), row.get('catalog_signature'), row.get('policy'),
            json.dumps(claim.get('qualifiers') or {}, sort_keys=True, ensure_ascii=False),
            evidence or row['candidate_id'],
            json.dumps({key: claim.get(key) for key in ('source_local_context', 'source_reading_context',
                       'source_context_packet', 'source_section_context', 'source_evidence_section_heading')},
                       sort_keys=True, ensure_ascii=False),
            json.dumps(row.get('abbreviation_definitions') or [], sort_keys=True, ensure_ascii=False))


def _groups(rows, roles):
    groups = defaultdict(list)
    for row in rows:
        claim = row['original_claim']
        for role in roles:
            key = (role, _context_key(row), _wording(claim[role]), _wording(claim.get(role + '_context')))
            if role == 'relation':
                key += (endpoint_identity(row, 'subject'), endpoint_identity(row, 'object'))
            groups[key].append(row)
    return groups


def _endpoint_source(row, role):
    claim = row['original_claim']
    return (claim.get('source_document_id'), claim.get('source_pdf_sha256'),
            tuple(claim.get('source_evidence_sentence_numbers') or ()),
            claim.get(role + '_source_mention'),
            (row.get('source_definition_context') or {}).get(role),
            row['bindings']['relation'].get('iri'))


def _update_alignment(row, role, mapping, reason, *, denotation=None):
    binding = row['bindings'][role]
    binding.update(mapping=mapping, reason=reason)
    if denotation is not None:
        binding['denotation'] = denotation
    if 'concept_key' in binding:
        # Indexed relation validation stores derived endpoint identities.
        from backend.evaluation.extract_then_ground import annotate_endpoint_identities
        annotate_endpoint_identities(row['original_claim'], row['bindings'])


def _reconcile_endpoint(siblings, role, decisions):
    iris, mappings, denotations = map(set, zip(*decisions))
    if (len(iris) != 1 or not all(iris)
            or not mappings <= {'exact', 'broader'}
            or not denotations <= {'concept', 'particular'}
            or (len(mappings) <= 1 and len(denotations) <= 1)):
        return False
    source = _endpoint_source(siblings[0], role)
    if not all(source[:4]) or any(
        row.get('policy') != ENTITY_GROUNDING_POLICY
        or row.get('scope') != 'in_scope'
        or row.get('mapping_review') != 'model_proposed'
        or review_status(row) in {'held', 'rejected'}
        or (row.get('property_type_check') or {}).get('status') != 'compatible'
        or any(w.get('role') == role for w in (row.get('mapping_review_warnings') or []))
        or not row['bindings']['relation'].get('fixed_from_extraction')
        or row['bindings'][role].get('under_review')
        or row['bindings'][role].get('components')
        or _endpoint_source(row, role) != source
        for row in siblings
    ):
        return False

    broader_ids = sorted(row['candidate_id'] for row in siblings if row['bindings'][role]['mapping'] == 'broader')
    target_mapping = 'broader' if len(mappings) > 1 else None
    target_denotation = 'concept' if len(denotations) > 1 else None
    for row in siblings:
        binding = row['bindings'][role]
        need_map = target_mapping is not None and binding['mapping'] != target_mapping
        need_den = target_denotation is not None and binding['denotation'] != target_denotation
        if not (need_map or need_den):
            continue
        binding['consistency_adjustment'] = {
            'policy': CONSISTENCY_POLICY,
            'original_mapping': binding['mapping'],
            'original_denotation': binding['denotation'],
            'original_reason': binding.get('reason'),
            **({'broader_candidate_ids': broader_ids} if broader_ids and need_map else {}),
        }
        reason = ('The same anchored endpoint received a broader alignment to this class in an identical context.'
                  if need_map else
                  'The same anchored endpoint was grounded to this class as a concept in an identical context.')
        _update_alignment(
            row, role,
            target_mapping or binding['mapping'],
            reason,
            denotation=target_denotation,
        )
    return True


def _flag_groups(groups, *, reconcile, policy):
    for key, siblings in groups.items():
        role = key[0]
        decisions = {(r['bindings'][role].get('iri'), r['bindings'][role]['mapping'],
                      r['bindings'][role].get('denotation')) for r in siblings}
        if len(decisions) > 1:
            if reconcile and role != 'relation' and _reconcile_endpoint(siblings, role, decisions):
                continue
            for row in siblings:
                row['mapping_consistency_warnings'].append({'role': role,
                    'message': 'Identical contextual input received conflicting mappings; decisions are preserved for review.',
                    'policy': policy,
                    'candidate_ids': sorted(r['candidate_id'] for r in siblings),
                    'decisions': [{'candidate_id': r['candidate_id'],
                                   'iri': r['bindings'][role].get('iri'),
                                   'mapping': r['bindings'][role]['mapping'],
                                   'denotation': r['bindings'][role].get('denotation')}
                                  for r in sorted(siblings, key=lambda r: r['candidate_id'])]})


def flag_consistency(rows, *, reconcile=True):
    policy = CONSISTENCY_POLICY if reconcile else PREVIOUS_CONSISTENCY_POLICY
    for row in rows:
        row['mapping_consistency_warnings'] = []
        # Start from the original decisions, including on checkpoint resume
        # when new siblings can change whether an adjustment is warranted.
        for role in ('subject', 'object'):
            binding = row['bindings'][role]
            adjustment = binding.pop('consistency_adjustment', None)
            if adjustment:
                _update_alignment(
                    row, role, adjustment['original_mapping'], adjustment['original_reason'],
                    denotation=adjustment.get('original_denotation'),
                )
    _flag_groups(_groups(rows, ('subject', 'object')), reconcile=reconcile, policy=policy)
    _flag_groups(_groups(rows, ('relation',)), reconcile=False, policy=policy)
    for row in rows:
        row['consistency_policy'] = policy
    return [refresh_eligibility(row) for row in rows]
