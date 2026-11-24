"""Classified coverage is independent of exact-predicate reasoning admission."""
from backend.domain.extraction_review import mapping_meaning_warnings, review_status
from backend.ontology.property_types import check_property_types
from backend.ontology.inventory_policy import retrieved_class
from backend.ontology.usage import direct_property_allowed

CLASSIFICATION_POLICY = 'ontology-classification-coverage-v6'


def binding_classified(binding, role, reference):
    if reference is None:
        return False
    allowed = reference.profile['allowed_object_properties' if role == 'relation' else 'allowed_classes']
    return bool(binding.get('mapping') in {'exact', 'broader'} and (binding.get('iri') in allowed or (role != 'relation' and retrieved_class(binding)))
                and (role != 'relation' or binding.get('mapping') != 'exact' or direct_property_allowed(binding.get('iri'))))


def row_mapping_warnings(row, reference):
    if hasattr(reference, 'terms'):
        return mapping_meaning_warnings(row, reference)
    return list(row.get('mapping_review_warnings') or [])


def warned_endpoint_roles(row, reference):
    return {w.get('role') for w in row_mapping_warnings(row, reference) if w.get('role') in ('subject', 'object')}


def endpoint_accepted(row, role, reference):
    return bool(binding_classified(row.get('bindings', {}).get(role, {}), role, reference)
                and role not in warned_endpoint_roles(row, reference))


def relation_property_type_status(row, reference):
    if hasattr(reference, 'terms'):
        return check_property_types(row, reference).get('status', 'not_applicable')
    return (row.get('property_type_check') or {}).get('status', 'not_applicable')


def relation_accepted(row, reference):
    rel_binding = row.get('bindings', {}).get('relation', {})
    if rel_binding.get('under_review'):
        return False
    warned_rel = any(w.get('role') == 'relation' for w in row_mapping_warnings(row, reference))
    return bool(not (row.get('original_claim') or {}).get('measured_comparison')
                and review_status(row) not in {'held', 'rejected'}
                and not warned_rel
                and not rel_binding.get('under_review')
                and binding_classified(rel_binding, 'relation', reference)
                and relation_property_type_status(row, reference) not in {'incompatible', 'missing_required_endpoint'})


def classification_complete(row, reference):
    return bool(not (row.get('original_claim') or {}).get('measured_comparison')
                and not any((b.get('components') or {}).get('grounding_completeness') in {'held', 'partial'} for b in row.get('bindings', {}).values())
                and row.get('scope') == 'in_scope' and review_status(row) not in {'held', 'rejected'}
                and not row.get('mapping_consistency_warnings')
                and not row_mapping_warnings(row, reference)
                and relation_accepted(row, reference)
                and all(endpoint_accepted(row, role, reference) for role in ('subject', 'object')))


def comparison_complete(row):
    return bool(row.get('scope') == 'in_scope' and row.get('comparison_assessment', {}).get('complete')
                and review_status(row) not in {'held', 'rejected'} and not row.get('mapping_consistency_warnings')
                and not row.get('mapping_review_warnings'))


def classification_counts(rows, reference):
    included = [r for r in rows if r.get('scope') == 'in_scope']
    exact = sum(r.get('bindings', {}).get('relation', {}).get('mapping') == 'exact'
                and relation_accepted(r, reference) for r in included)
    broader = sum(r.get('bindings', {}).get('relation', {}).get('mapping') == 'broader'
                  and relation_accepted(r, reference) for r in included)
    proposed_under_review = sum(
        binding_classified(r.get('bindings', {}).get('relation', {}), 'relation', reference)
        and not relation_accepted(r, reference) for r in included)
    type_conflicts = sum(relation_property_type_status(r, reference) == 'incompatible' for r in included)
    missing_endpoint = sum(relation_property_type_status(r, reference) == 'missing_required_endpoint' for r in included)
    type_reviews = sum(relation_property_type_status(r, reference) == 'insufficient_type_info' for r in included)
    endpoint_warns = sum(len(warned_endpoint_roles(r, reference)) for r in included)
    mapping_warns = sum(bool(row_mapping_warnings(r, reference)) for r in included)
    return {'measured_comparisons': sum(bool(r.get('comparison_assessment')) for r in included),
            'complete_measured_comparisons': sum(comparison_complete(r) for r in included),
            'classified_endpoint_mentions': sum(endpoint_accepted(r, k, reference)
                for r in included for k in ('subject', 'object')),
            'endpoint_review_warning_mentions': endpoint_warns,
            'mapping_review_warning_claims': mapping_warns,
            'classified_relation_mentions': exact + broader, 'exact_relation_mentions': exact,
            'broader_relation_mentions': broader,
            'proposed_relation_under_review_mentions': proposed_under_review,
            'property_type_conflict_claims': type_conflicts,
            'property_type_missing_endpoint_claims': missing_endpoint,
            'property_type_review_claims': type_reviews,
            'classified_claims': sum(classification_complete(r, reference) for r in rows),
            'classification_review_claims': sum(r.get('scope') != 'out_of_scope'
                and review_status(r) != 'rejected' and not classification_complete(r, reference) and not comparison_complete(r) for r in rows)}
