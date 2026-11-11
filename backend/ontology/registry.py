"""Keep endpoint identity independent of complete-assertion eligibility."""
from backend.domain.extraction_review import can_share


def entity_registry(assertions):
    entities = {}
    for row in assertions:
        if row['scope'] != 'in_scope':
            continue
        for role in ('subject', 'object'):
            binding = row['bindings'][role]
            grounded = binding['mapping'] == 'exact' and bool(binding.get('iri'))
            endpoint_context = row['original_claim'].get(role + '_context') or ''
            shared = bool(can_share(row, role))
            # A common type/parent never merges distinct occurrences or states.
            key = binding['entity_id'] if shared else binding['occurrence_id']
            entity = entities.setdefault(key, {
                'id': key, 'label': binding['label'] if shared else row['original_claim'][role],
                'iri': binding.get('iri'), 'concept_label': binding.get('label'),
                'kind': binding['denotation'], 'mapping': binding['mapping'],
                'status': 'grounded' if grounded else 'vocabulary_gap' if binding['mapping'] == 'unresolved' else 'alignment_review',
                'alignment_category': ('exact' if grounded else 'classification' if binding['mapping'] == 'broader'
                                       else 'unresolved' if binding['mapping'] == 'unresolved' else 'review'),
                'shared_concept': shared, 'occurrences': []})
            entity['occurrences'].append({
                'id': binding['occurrence_id'], 'document_key': row['document_key'],
                'candidate_id': row['candidate_id'], 'role': role,
                'source_mention': row['original_claim'].get(role + '_source_mention') or row['original_claim'][role],
                'source_page': row['original_claim'].get('source_page'),
                'endpoint_context': endpoint_context,
                'claim_assembly_eligible': row['assembly_eligible']})
    return list(entities.values())
