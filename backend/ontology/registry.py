"""Keep endpoint and relation identity independent of complete-assertion eligibility."""
from collections import Counter
import json

from backend.domain.extraction_review import can_share, assertion_eligible
from backend.shared.storage import stable_hash


def entity_registry(assertions):
    entities = {}
    for row in assertions:
        if row['scope'] != 'in_scope':
            continue
        warned_roles = {w.get('role') for w in (row.get('mapping_review_warnings') or [])}
        for role in ('subject', 'object'):
            binding = row['bindings'][role]
            role_warned = role in warned_roles
            grounded = binding['mapping'] == 'exact' and bool(binding.get('iri')) and not role_warned
            endpoint_context = row['original_claim'].get(role + '_context') or ''
            shared = bool(can_share(row, role))
            # A common type/parent never merges distinct occurrences or states.
            key = binding['entity_id'] if shared else binding['occurrence_id']
            entity = entities.setdefault(key, {
                'id': key, 'label': binding['label'] if shared else row['original_claim'][role],
                'iri': binding.get('iri'), 'concept_label': binding.get('label'),
                'kind': binding['denotation'], 'mapping': binding['mapping'],
                'status': 'grounded' if grounded else 'vocabulary_gap' if binding['mapping'] == 'unresolved' else 'alignment_review',
                'alignment_category': ('review' if role_warned else 'exact' if grounded else
                                       'classification' if binding['mapping'] == 'broader'
                                       else 'unresolved' if binding['mapping'] == 'unresolved' else 'review'),
                'shared_concept': shared, 'occurrences': []})
            entity['occurrences'].append({
                'id': binding['occurrence_id'], 'document_key': row['document_key'],
                'candidate_id': row['candidate_id'], 'role': role,
                'source_mention': row['original_claim'].get(role + '_source_mention') or row['original_claim'][role],
                'source_page': row['original_claim'].get('source_page'),
                'endpoint_context': endpoint_context,
                'claim_assembly_eligible': assertion_eligible(row)})
    return list(entities.values())


def relation_registry(assertions, ref, *, scope='in_scope', document=None):
    """Aggregate extracted relation mappings and unused catalog object properties."""
    groups = {}
    variant_counts = {}
    used_canonical = set()
    total_claims = 0
    grounded_mentions = 0
    broader_mentions = 0
    unresolved_mentions = 0

    for row in assertions:
        if (row.get('original_claim') or {}).get('measured_comparison'):
            continue
        if scope != 'all' and row.get('scope') != scope:
            continue
        if document and row.get('document_key') != document:
            continue
        total_claims += 1
        claim = row.get('original_claim') or {}
        binding = (row.get('bindings') or {}).get('relation') or {}
        raw_rel = (claim.get('relation') or '').strip()
        approved = binding.get('iri') in ref.profile['allowed_object_properties']
        grounded = binding.get('mapping') == 'exact' and approved
        broader = binding.get('mapping') == 'broader' and approved

        if grounded or broader:
            grounded_mentions += int(grounded)
            broader_mentions += int(broader)
            raw_iri = binding['iri']
            canon = binding.get('canonical_iri') or ref.canonical_equivalent(raw_iri)
            term = ref.terms.get(canon) or ref.terms.get(raw_iri) or {}
            prop_iri = term.get('iri') or canon or raw_iri
            used_canonical.add(canon)
            used_canonical.add(prop_iri)
            status = 'grounded' if grounded else 'classification'
            key = (status, canon)
            label = term.get('label') or binding.get('label') or prop_iri
            term_id = term.get('id', '')
            definition = term.get('definition', '')
            mapping = 'exact' if grounded else 'broader'
        else:
            unresolved_mentions += 1
            norm_rel = raw_rel.casefold()
            prop_iri = None
            key = ('unresolved', norm_rel)
            label = raw_rel or '(empty relation)'
            term_id = ''
            definition = ''
            status = 'unresolved'
            mapping = 'unresolved'

        if key not in groups:
            groups[key] = {
                'id': 'rel-' + stable_hash(f'{key[0]}|{key[1]}', 24),
                'status': status,
                'mapping': mapping,
                'iri': prop_iri,
                'term_id': term_id,
                'label': label,
                'definition': definition,
                'occurrences': [],
            }
            variant_counts[key] = Counter()

        phrase = raw_rel or '(empty relation)'
        variant_counts[key][phrase] += 1
        groups[key]['occurrences'].append({
            'id': 'rel-occ-' + stable_hash(f"{row['document_key']}|{row['candidate_id']}", 24),
            'document_key': row['document_key'],
            'candidate_id': row['candidate_id'],
            'scope': row.get('scope'),
            'source_page': claim.get('source_page'),
            'relation_phrase': raw_rel,
            'reason': binding.get('reason', ''),
            'claim_assembly_eligible': assertion_eligible(row),
        })

    extracted = []
    for key, entry in groups.items():
        variants = sorted(variant_counts[key].items(), key=lambda kv: (-kv[1], kv[0].casefold()))
        entry['raw_variants'] = [{'label': phrase, 'count': count} for phrase, count in variants]
        if entry['status'] == 'unresolved' and variants:
            entry['label'] = variants[0][0]
        entry['occurrence_count'] = len(entry['occurrences'])
        entry['sample_reason'] = next((o['reason'] for o in entry['occurrences'] if o.get('reason')), '')
        extracted.append(entry)

    extracted.sort(key=lambda item: (-item['occurrence_count'], item['label'].casefold(), item['id']))

    selectable_props = [
        t for t in ref.terms.values()
        if t.get('kind') == 'object_property' and t.get('selectable') and not t.get('deprecated')
    ]
    unused = []
    for term in selectable_props:
        canon = ref.canonical_equivalent(term['iri'])
        if term['iri'] in used_canonical or canon in used_canonical:
            continue
        unused.append({
            'id': 'rel-unused-' + stable_hash(term['iri'], 24),
            'status': 'unused',
            'mapping': 'unused',
            'iri': term['iri'],
            'term_id': term.get('id', ''),
            'label': term.get('label', term['iri']),
            'definition': term.get('definition', ''),
            'raw_variants': [],
            'occurrence_count': 0,
            'sample_reason': '',
            'occurrences': [],
        })
    unused.sort(key=lambda item: (item['label'].casefold(), item['iri']))

    summary = {
        'total_claims': total_claims,
        'grounded_mentions': grounded_mentions,
        'broader_mentions': broader_mentions,
        'classified_mentions': grounded_mentions + broader_mentions,
        'classified_properties': len(used_canonical.intersection({ref.canonical_equivalent(t['iri']) for t in selectable_props})),
        'unresolved_mentions': unresolved_mentions,
        'used_properties': sum(1 for item in extracted if item['status'] == 'grounded'),
        'catalog_properties': len(selectable_props),
        'unused_properties': len(unused),
        'unresolved_phrases': sum(1 for item in extracted if item['status'] == 'unresolved'),
    }
    return {'extracted': extracted, 'unused': unused, 'summary': summary}


def extraction_inventory(root):
    """Active claim identities, not counts inferred from grounded property groups."""
    from backend.shared.storage import read_jsonl
    paths = sorted(root.glob('*/candidates.jsonl'))
    if not paths:
        return None
    return [{'document_key': path.parent.name, 'candidate_id': row['candidate_id']}
            for path in paths for row in read_jsonl(path)
            if not row.get('schema_error') and not row.get('superseded_by') and not row.get('measured_comparison')]


def property_relations(assertions, ref, *, scope='in_scope', document=None, q='', status='classified',
                       offset=0, limit=20, inventory=None):
    """Display aggregation only: exact and broader occurrences keep separate decisions."""
    needle = q.strip().casefold()
    scoped = [r for r in assertions if not (r.get('original_claim') or {}).get('measured_comparison')
              and (scope == 'all' or r.get('scope') == scope)
              and (not document or r['document_key'] == document)]
    filtered = []
    for row in scoped:
        binding = row.get('bindings', {}).get('relation', {})
        term = ref.terms.get(binding.get('iri'), {})
        haystack = [row.get('original_claim'), row.get('candidate_id'), binding.get('reason'),
                    term.get('label'), term.get('id'), term.get('iri'), term.get('definition')]
        if not needle or needle in json.dumps(haystack, ensure_ascii=False).casefold():
            filtered.append(row)
    built = relation_registry(filtered, ref, scope='all')
    counts = {'all': len(filtered), 'grounded': built['summary']['grounded_mentions'],
              'classification': built['summary']['broader_mentions'],
              'classified': built['summary']['classified_mentions'], 'unresolved': built['summary']['unresolved_mentions']}
    groups = {}
    for item in built['extracted']:
        if status != 'all' and (item['status'] not in {'grounded', 'classification'} if status == 'classified'
                                else item['status'] != status):
            continue
        key = item['iri'] or 'unresolved:' + ' '.join(item['label'].casefold().split())
        if key not in groups:
            groups[key] = {**item, 'id': 'property-' + stable_hash(key, 24), 'occurrences': [],
                           'exact_count': 0, 'broader_count': 0, 'raw_variants': []}
        group = groups[key]
        group['exact_count'] += item['occurrence_count'] if item['mapping'] == 'exact' else 0
        group['broader_count'] += item['occurrence_count'] if item['mapping'] == 'broader' else 0
        group['occurrences'].extend({**o, 'mapping': item['mapping']} for o in item['occurrences'])
        group['raw_variants'].extend(item['raw_variants'])
    for group in groups.values():
        variants = Counter()
        for v in group['raw_variants']:
            variants[v['label']] += v['count']
        group['raw_variants'] = [{'label': label, 'count': n} for label, n in variants.most_common()]
        group['occurrence_count'] = len(group['occurrences'])
    ordered = sorted(groups.values(), key=lambda g: (-g['occurrence_count'], g['label'].casefold()))
    scope_rows = [r for r in assertions if not document or r['document_key'] == document]
    identities = {(r['document_key'], r['candidate_id']) for r in scope_rows}
    selected_inventory = None if inventory is None else {(r['document_key'], r['candidate_id']) for r in inventory
                                                        if not document or r['document_key'] == document}
    return {'relations': ordered[offset:offset + limit], 'total': len(ordered), 'offset': offset, 'limit': limit,
            'summary': built['summary'], 'mention_counts': counts, 'group_by': 'property',
            'represented_claims': sum(g['occurrence_count'] for g in ordered),
            'extracted_claim_relations': None if selected_inventory is None else len(selected_inventory),
            'awaiting_grounding_claims': None if selected_inventory is None else len(selected_inventory - identities),
            'scope_counts': dict(Counter(r.get('scope') for r in scope_rows))}
