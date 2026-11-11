"""Conservative review signals, never automatic scientific corrections."""
import re

from backend.shared.storage import stable_hash

REVIEW_POLICY = 'sentence-level-extraction-review-v2'
POSITIVE = re.compile(r'\b(?:increas\w*|enhanc\w*|promot\w*|improv\w*|absor\w*|acquir\w*|caus\w*|reduc\w*|decreas\w*)\b', re.I)
NEGATIVE = re.compile(r'\b(?:cannot|can\s+not|could\s+not|does\s+not|did\s+not|no\s+significant\s+(?:effect|difference|increase|decrease)|not\s+(?:statistically\s+)?significant)\b', re.I)
RESTRICTIVE = re.compile(r'\bonly\s+(?:\d+|one|two|three|four|five)\s+(?:out\s+of|of)\s+(?:\d+|\w+)\b', re.I)
LIMITATION = re.compile(r'\b(?:limited\s+availability\s+of|scarcity\s+of|deficiency\s+of|lack\s+of|low\s+availability\s+of)\s+', re.I)


def extraction_warnings(claim):
    """Sentence-level review signals; no clause splitting and no automatic corrections."""
    warnings = []
    relation = str(claim.get('relation') or '')
    kept = ' '.join(str(claim.get(k) or '') for k in ('subject', 'relation', 'object')) + ' ' + ' '.join(
        str(v) for v in (claim.get('qualifiers') or {}).values() if v)
    fragments = claim.get('source_evidence_target_fragments') or claim.get('source_evidence_fragments') \
        or [{'text': claim.get('source_evidence_span', '')}]
    def add(code, text, sentence):
        if not any(w['code'] == code for w in warnings):
            warnings.append({'code': code, 'message': text, 'sentence_number': sentence.get('sentence_number'),
                             'source_text': sentence['text'], 'policy': REVIEW_POLICY})
    for sentence in fragments:
        text = sentence['text']
        if NEGATIVE.search(text) and not NEGATIVE.search(kept) and POSITIVE.search(relation):
            add('possible_lost_negation', 'The cited sentence contains a negation or non-significance that the claim does not keep.', sentence)
        if RESTRICTIVE.search(text) and not RESTRICTIVE.search(kept):
            add('possible_generalized_subset', 'The cited sentence restricts the finding to a subset that the claim does not keep.', sentence)
        if LIMITATION.search(text) and not LIMITATION.search(kept) and not re.search(r'\b(?:limited|low|deficien\w*|scarc\w*)\b', kept, re.I):
            add('possible_lost_defining_state', 'The cited sentence names a limitation or deficiency state that the claim does not keep.', sentence)
    return warnings


def review_status(row):
    review = row.get('extraction_review') or {}
    if review.get('claim_signature') == row.get('claim_signature') and review.get('status') in {'accepted', 'rejected'}:
        return review['status']
    return 'held' if row.get('extraction_warnings') or row.get('mapping_consistency_warnings') else 'not_flagged'


def can_share(row, role):
    binding = row['bindings'][role]
    qualifiers = row['original_claim'].get('qualifiers') or {}
    return (review_status(row) not in {'held', 'rejected'} and not row.get('mapping_consistency_warnings')
            and binding.get('iri') and binding['mapping'] == 'exact' and binding['denotation'] == 'concept'
            and not row['original_claim'].get(role + '_context')
            and not any(qualifiers.get(k) for k in ('population', 'condition', 'comparator', 'metric')))


def refresh_eligibility(row):
    row['extraction_review_status'] = review_status(row)
    for role in ('subject', 'object'):
        binding = row['bindings'][role]
        binding['entity_id'] = ('concept-' + stable_hash(binding['canonical_iri'], 32)
                                if can_share(row, role) else binding['occurrence_id'])
    row['assembly_eligible'] = (row['scope'] == 'in_scope' and review_status(row) not in {'held', 'rejected'}
                               and not row.get('mapping_consistency_warnings')
                               and all(row['bindings'][r]['mapping'] in {'exact', 'broader'} and row['bindings'][r].get('iri')
                                       for r in ('subject', 'object')))
    return row
