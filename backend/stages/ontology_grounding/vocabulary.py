"""Project frozen upstream annotations into grounding packet aliases."""
from copy import deepcopy

from backend.ontology.property_types import verifiable_upper_types


def _type_labels(catalog, values):
    return sorted({catalog.terms.get(value, {}).get('label', value)
                   if str(value).startswith(('http://', 'https://'))
                   else '[unexpanded anonymous OWL restriction]'
                   for value in values})


def _axiom_sources(records):
    """Cite one upstream source per distinct edge in the prompt. The catalogue retains every source."""
    edges = {}
    for record in sorted(records, key=lambda r: (len(r['source']), r['source'])):
        edge = (record['subject'], record['object'])
        edges.setdefault(edge, {'subject': edge[0].rsplit('/', 1)[-1], 'object': edge[1].rsplit('/', 1)[-1],
                                'source': record['source']})
    return [edges[key] for key in sorted(edges)]


def catalog_term_map(reference, *, selected_properties=None, include_classes=True):
    catalog = getattr(reference, 'catalog', reference)
    terms, term_iris = {}, {}
    for iri in (catalog.profile['allowed_classes'] if include_classes else ()):
        term = catalog.terms[iri]
        if not term['selectable'] or term['kind'] != 'class' or term['deprecated']:
            continue
        alias = f'T{len(terms) + 1}'
        entry = {'label': term['label']}
        if term.get('definition'):
            entry['definition'] = term['definition']
        if term.get('exact_synonyms'):
            entry['exact_synonyms'] = deepcopy(term['exact_synonyms'])
        if term.get('related_synonyms'):
            entry['related_synonyms'] = deepcopy(term['related_synonyms'])
        # Anonymous OWL restrictions have process-local blank-node identifiers,
        # not selectable concepts or meaningful names for the model.
        entry['parents'] = sorted(catalog.terms.get(parent, {}).get('label', parent)
                                  for parent in term.get('parents', [])
                                  if str(parent).startswith(('http://', 'https://')))
        upper = verifiable_upper_types(catalog, iri)
        if upper:
            entry['upper_types'] = upper
        terms[alias] = entry
        term_iris[alias] = iri
    if selected_properties is None:
        extensions = set(catalog.profile.get('retrieval_object_properties', ()))
        selected_properties = set(catalog.profile['allowed_object_properties']) - extensions
    relation_aliases = {
        iri: f'R{i}'
        for i, iri in enumerate(catalog.profile['allowed_object_properties'], 1)
        if iri in selected_properties
    }
    relations, relation_iris = {}, {}
    for iri, alias in relation_aliases.items():
        term = catalog.terms[iri]
        entry = {'label': term['label']}
        if term.get('definition'):
            entry['definition'] = term['definition']
        for field in ('usage_notes', 'comments', 'obo_unique_labels', 'allowed_mappings'):
            if term.get(field):
                entry[field] = deepcopy(term[field])
        for kind in ('domain', 'range'):
            entry[kind] = _type_labels(catalog, term.get(kind, []))
        if term.get('parent_properties'):
            covered_by_selectable = {
                grand['iri']
                for p in term['parent_properties']
                if p['iri'] in relation_aliases
                for grand in catalog.terms.get(p['iri'], {}).get('parent_properties', [])
            }
            entry['parent_properties'] = []
            kept_iris = {iri}
            for anc in term['parent_properties']:
                if anc['iri'] in covered_by_selectable:
                    continue
                kept_iris.add(anc['iri'])
                if anc['iri'] in relation_aliases:
                    parent_entry = {
                        'iri': anc['iri'],
                        'ref': relation_aliases[anc['iri']],
                        'label': anc['label'],
                    }
                    if anc.get('usage_notes'):
                        parent_entry['usage_notes'] = deepcopy(anc['usage_notes'])
                else:
                    parent_entry = {'iri': anc['iri'], 'label': anc['label']}
                    if anc.get('definition'):
                        parent_entry['definition'] = anc['definition']
                    for field in ('usage_notes', 'comments', 'obo_unique_labels', 'allowed_mappings'):
                        if anc.get(field):
                            parent_entry[field] = deepcopy(anc[field])
                for kind in ('domain', 'range'):
                    if anc.get(kind):
                        parent_entry[kind] = _type_labels(catalog, anc[kind])
                entry['parent_properties'].append(parent_entry)
            if term.get('subproperty_provenance'):
                entry['subproperty_provenance'] = _axiom_sources(
                    p for p in term['subproperty_provenance']
                    if p['subject'] in kept_iris and p['object'] in kept_iris
                )
        if term.get('inverse_of'):
            entry['inverse_of'] = []
            for peer in term['inverse_of']:
                linked = catalog.terms.get(peer, {})
                inverse = {'iri': peer, 'label': linked.get('label', peer)}
                for field in ('definition', 'usage_notes', 'comments', 'obo_unique_labels'):
                    if linked.get(field):
                        inverse[field] = deepcopy(linked[field])
                for kind in ('domain', 'range'):
                    inverse[kind] = _type_labels(catalog, linked.get(kind, []))
                entry['inverse_of'].append(inverse)
            entry['inverse_provenance'] = _axiom_sources(term.get('inverse_provenance', []))
        relations[alias] = entry
        relation_iris[alias] = iri
    return catalog, terms, term_iris, relations, relation_iris
