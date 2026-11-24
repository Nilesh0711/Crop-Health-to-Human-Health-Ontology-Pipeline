"""Verify relation domain, range, inherited, and inverse type requirements from imported RDF."""
from __future__ import annotations

from backend.ontology.inventory_policy import retrieved_class
from rdflib import URIRef
from rdflib.namespace import OWL

PROPERTY_TYPE_POLICY = 'imported-domain-range-inverse-check-v2'

BFO_2020_CORE_SOURCE = 'https://raw.githubusercontent.com/BFO-ontology/BFO-2020/master/21838-2/owl/bfo-core.ttl'
BFO_PARTITION_AXIOMS = {
    'continuant_vs_occurrent': {
        'axiom': 'BFO:0000002 owl:disjointWith BFO:0000003',
        'source': BFO_2020_CORE_SOURCE,
    },
    'independent_vs_specifically_dependent_continuant': {
        'axiom': 'BFO:0000004 owl:disjointWith BFO:0000020',
        'source': BFO_2020_CORE_SOURCE,
    },
}

BFO_CONTINUANT = 'http://purl.obolibrary.org/obo/BFO_0000002'
BFO_OCCURRENT = 'http://purl.obolibrary.org/obo/BFO_0000003'
BFO_INDEPENDENT_CONTINUANT = 'http://purl.obolibrary.org/obo/BFO_0000004'
BFO_PROCESS = 'http://purl.obolibrary.org/obo/BFO_0000015'
BFO_QUALITY = 'http://purl.obolibrary.org/obo/BFO_0000019'
BFO_SDC = 'http://purl.obolibrary.org/obo/BFO_0000020'
BFO_GDC = 'http://purl.obolibrary.org/obo/BFO_0000031'
BFO_MATERIAL_ENTITY = 'http://purl.obolibrary.org/obo/BFO_0000040'
PATO_CHARACTERISTIC = 'http://purl.obolibrary.org/obo/PATO_0000001'
GO_BIOLOGICAL_PROCESS = 'http://purl.obolibrary.org/obo/GO_0008150'
GO_MOLECULAR_FUNCTION = 'http://purl.obolibrary.org/obo/GO_0003674'
GO_CELLULAR_COMPONENT = 'http://purl.obolibrary.org/obo/GO_0005575'

VERIFIABLE_UPPER_IRIS = (
    BFO_CONTINUANT,
    BFO_OCCURRENT,
    BFO_INDEPENDENT_CONTINUANT,
    BFO_PROCESS,
    BFO_QUALITY,
    BFO_SDC,
    BFO_GDC,
    BFO_MATERIAL_ENTITY,
    PATO_CHARACTERISTIC,
    GO_BIOLOGICAL_PROCESS,
    GO_MOLECULAR_FUNCTION,
    GO_CELLULAR_COMPONENT,
)

_OCCURRENT_IRIS = frozenset({BFO_OCCURRENT, BFO_PROCESS})
_CONTINUANT_IRIS = frozenset({
    BFO_CONTINUANT,
    BFO_INDEPENDENT_CONTINUANT,
    BFO_MATERIAL_ENTITY,
    BFO_QUALITY,
    BFO_SDC,
    BFO_GDC,
})
_INDEPENDENT_IRIS = frozenset({BFO_INDEPENDENT_CONTINUANT, BFO_MATERIAL_ENTITY})
_SDC_IRIS = frozenset({BFO_SDC, BFO_QUALITY})


def class_ancestors(reference, iri: str | None) -> set[str]:
    """Return transitive named ancestor IRIs (including ``iri``) from imported RDF."""
    if not iri or not hasattr(reference, 'terms'):
        return set()
    terms = reference.terms
    seen: set[str] = set()
    stack = [iri]
    while stack:
        current = stack.pop()
        if current in seen or not str(current).startswith(('http://', 'https://')):
            continue
        seen.add(current)
        for parent in (terms.get(current) or {}).get('parents', []):
            if str(parent).startswith(('http://', 'https://')) and parent not in seen:
                stack.append(parent)
    return seen


def verifiable_upper_types(reference, iri: str | None) -> list[str]:
    """Return sorted human labels for verifiable upper classes in ``iri``'s ancestry."""
    if not iri or not hasattr(reference, 'terms'):
        return []
    ancestors = class_ancestors(reference, iri)
    terms = reference.terms
    labels = {
        terms[upper]['label']
        for upper in VERIFIABLE_UPPER_IRIS
        if upper != iri and upper in ancestors and terms.get(upper, {}).get('label')
    }
    return sorted(labels)


def _partition_values(values) -> tuple[set[str], set[str]]:
    http_vals, unsupported = set(), set()
    for v in (values or ()):
        s = str(v).strip()
        if not s:
            continue
        if s.startswith(('http://', 'https://')):
            http_vals.add(s)
        else:
            unsupported.add(s)
    return http_vals, unsupported


def _property_closure(terms: dict, root_iri: str) -> list[dict]:
    """Return term dicts for ``root_iri`` and all transitive superproperties."""
    ordered: list[dict] = []
    seen_iris: set[str] = set()
    stack: list[str] = [root_iri]
    while stack:
        current = stack.pop()
        if not current or current in seen_iris:
            continue
        seen_iris.add(current)
        term = terms.get(current)
        if isinstance(term, dict):
            ordered.append(term)
            for p_iri in term.get('parents', []):
                if p_iri and p_iri not in seen_iris:
                    stack.append(p_iri)
            for p_dict in term.get('parent_properties', []):
                if not isinstance(p_dict, dict):
                    continue
                p_iri = p_dict.get('iri')
                if p_iri:
                    if p_iri not in seen_iris:
                        if p_iri in terms:
                            stack.append(p_iri)
                        else:
                            seen_iris.add(p_iri)
                            ordered.append(p_dict)
                else:
                    ordered.append(p_dict)
    return ordered


def effective_property_restrictions(reference, prop_iri: str) -> dict[str, list[str]]:
    """Return effective domain and range IRIs (and unsupported OWL expressions) from direct, inherited, and inverse axioms."""
    if not prop_iri or not hasattr(reference, 'terms'):
        return {'domain': [], 'range': [], 'unsupported_domain': [], 'unsupported_range': []}
    terms = reference.terms
    domain: set[str] = set()
    rng: set[str] = set()
    unsupported_domain: set[str] = set()
    unsupported_range: set[str] = set()

    for prop_term in _property_closure(terms, prop_iri):
        d_http, d_unsup = _partition_values(prop_term.get('domain'))
        r_http, r_unsup = _partition_values(prop_term.get('range'))
        domain.update(d_http)
        unsupported_domain.update(d_unsup)
        rng.update(r_http)
        unsupported_range.update(r_unsup)

        for inv_iri in prop_term.get('inverse_of', []):
            for inv_term in _property_closure(terms, inv_iri):
                inv_d_http, inv_d_unsup = _partition_values(inv_term.get('domain'))
                inv_r_http, inv_r_unsup = _partition_values(inv_term.get('range'))
                domain.update(inv_r_http)
                unsupported_domain.update(inv_r_unsup)
                rng.update(inv_d_http)
                unsupported_range.update(inv_d_unsup)

    return {
        'domain': sorted(domain),
        'range': sorted(rng),
        'unsupported_domain': sorted(unsupported_domain),
        'unsupported_range': sorted(unsupported_range),
    }


def _disjoint_pairs(reference) -> set[tuple[str, str]]:
    cached = getattr(reference, '_cached_disjoint_pairs', None)
    if cached is not None:
        return cached
    graph = getattr(reference, 'graph', None)
    pairs: set[tuple[str, str]] = set()
    if graph is not None:
        for subj, obj in graph.subject_objects(OWL.disjointWith):
            if isinstance(subj, URIRef) and isinstance(obj, URIRef):
                s_iri, o_iri = str(subj), str(obj)
                if s_iri.startswith(('http://', 'https://')) and o_iri.startswith(('http://', 'https://')):
                    pairs.add((s_iri, o_iri))
                    pairs.add((o_iri, s_iri))
    try:
        setattr(reference, '_cached_disjoint_pairs', pairs)
    except Exception:
        pass
    return pairs


def _bfo_partition_conflict_axiom(ancestors: set[str], req_iri: str) -> dict | None:
    is_occ = bool(_OCCURRENT_IRIS & ancestors)
    is_cont = bool(_CONTINUANT_IRIS & ancestors)
    is_ind = bool(_INDEPENDENT_IRIS & ancestors)
    is_sdc = bool(_SDC_IRIS & ancestors)
    if (req_iri in _CONTINUANT_IRIS and is_occ) or (req_iri in _OCCURRENT_IRIS and is_cont):
        return BFO_PARTITION_AXIOMS['continuant_vs_occurrent']
    if (req_iri in _SDC_IRIS and is_ind) or (req_iri in _INDEPENDENT_IRIS and is_sdc):
        return BFO_PARTITION_AXIOMS['independent_vs_specifically_dependent_continuant']
    return None


def _bfo_partition_conflict(ancestors: set[str], req_iri: str) -> bool:
    return _bfo_partition_conflict_axiom(ancestors, req_iri) is not None


def _type_labels(reference, iris: list[str]) -> list[str]:
    terms = getattr(reference, 'terms', {}) or {}
    return [terms.get(iri, {}).get('label') or iri.rsplit('/', 1)[-1] for iri in iris]


def check_property_types(row: dict, reference) -> dict:
    """Validate mapped subject/object types against effective property restrictions."""
    if not hasattr(reference, 'terms'):
        existing = row.get('property_type_check')
        if isinstance(existing, dict) and existing.get('status'):
            return existing
        return {
            'status': 'not_applicable',
            'policy': PROPERTY_TYPE_POLICY,
            'has_unsupported_restrictions': False,
            'issues': [],
        }

    rel = (row.get('bindings') or {}).get('relation') or {}
    prop_iri = rel.get('iri')
    if rel.get('mapping') not in {'exact', 'broader'} or not prop_iri or prop_iri not in reference.terms:
        return {
            'status': 'not_applicable',
            'policy': PROPERTY_TYPE_POLICY,
            'has_unsupported_restrictions': False,
            'issues': [],
        }

    reqs = effective_property_restrictions(reference, prop_iri)
    disjoint = _disjoint_pairs(reference)
    terms = reference.terms
    issues: list[dict] = []

    for role, req_key, unsup_key in (
        ('subject', 'domain', 'unsupported_domain'),
        ('object', 'range', 'unsupported_range'),
    ):
        required_iris = reqs[req_key]
        unsupported = reqs[unsup_key]
        if not required_iris and not unsupported:
            continue
        binding = (row.get('bindings') or {}).get(role) or {}
        ep_iri = binding.get('iri')
        ep_mapping = binding.get('mapping') or 'unresolved'
        if ep_mapping not in {'exact', 'broader'} or not ep_iri or (ep_iri not in terms and not retrieved_class(binding)):
            req_desc = ', '.join(_type_labels(reference, required_iris)) if required_iris else 'anonymous OWL class expression'
            issues.append({
                'role': role,
                'kind': 'missing_required_endpoint',
                'endpoint_mapping': ep_mapping,
                'endpoint_iri': ep_iri,
                'endpoint_label': binding.get('label'),
                'required_iris': required_iris,
                'required_labels': _type_labels(reference, required_iris),
                'unsupported_expressions': len(unsupported),
                'message': f"{role} lacks a classified ontology class (mapping='{ep_mapping}') to verify {req_key} requirement ({req_desc}).",
            })
            continue

        ep_label = (terms.get(ep_iri) or {}).get('label') or binding.get('label') or ep_iri.rsplit('/', 1)[-1]
        if required_iris:
            ancestors = class_ancestors(reference, ep_iri)
            if retrieved_class(binding):
                ancestors.update(binding.get('verified_ancestor_iris', ()))
            unmet = [r for r in required_iris if r not in ancestors]
            if unmet:
                conflict_reqs = []
                conflict_sources = []
                for req_iri in unmet:
                    req_anc = class_ancestors(reference, req_iri)
                    bfo_ax = _bfo_partition_conflict_axiom(ancestors, req_iri)
                    if bfo_ax is not None:
                        conflict_reqs.append(req_iri)
                        if bfo_ax not in conflict_sources:
                            conflict_sources.append(bfo_ax)
                    elif any((a, ra) in disjoint for a in ancestors for ra in req_anc):
                        conflict_reqs.append(req_iri)
                        rdf_src = {'axiom': 'owl:disjointWith', 'source': 'imported_rdf_fragment'}
                        if rdf_src not in conflict_sources:
                            conflict_sources.append(rdf_src)

                if conflict_reqs:
                    req_labels = _type_labels(reference, conflict_reqs)
                    issues.append({
                        'role': role,
                        'kind': 'incompatible',
                        'endpoint_iri': ep_iri,
                        'endpoint_label': ep_label,
                        'required_iris': conflict_reqs,
                        'required_labels': req_labels,
                        'axiom_source': conflict_sources[0],
                        'axiom_sources': conflict_sources,
                        'message': f"{role} class '{ep_label}' conflicts with property {req_key} requirement ({', '.join(req_labels)}).",
                    })
                else:
                    req_labels = _type_labels(reference, unmet)
                    issues.append({
                        'role': role,
                        'kind': 'insufficient_type_info',
                        'endpoint_iri': ep_iri,
                        'endpoint_label': ep_label,
                        'required_iris': unmet,
                        'required_labels': req_labels,
                        'message': f"{role} class '{ep_label}' lacks imported upper-type link to verify {req_key} requirement ({', '.join(req_labels)}).",
                    })

        if unsupported:
            issues.append({
                'role': role,
                'kind': 'unsupported_expression',
                'endpoint_iri': ep_iri,
                'endpoint_label': ep_label,
                'unsupported_expressions': unsupported,
                'message': f"property declares an unsupported anonymous OWL {req_key} expression that cannot be verified automatically.",
            })

    if any(issue['kind'] == 'incompatible' for issue in issues):
        status = 'incompatible'
    elif any(issue['kind'] == 'missing_required_endpoint' for issue in issues):
        status = 'missing_required_endpoint'
    elif any(issue['kind'] in {'insufficient_type_info', 'unsupported_expression'} for issue in issues):
        status = 'insufficient_type_info'
    else:
        status = 'compatible'

    return {
        'status': status,
        'policy': PROPERTY_TYPE_POLICY,
        'property_iri': prop_iri,
        'effective_domain': _type_labels(reference, reqs['domain']),
        'effective_range': _type_labels(reference, reqs['range']),
        'has_unsupported_restrictions': bool(reqs['unsupported_domain'] or reqs['unsupported_range']),
        'issues': issues,
    }
