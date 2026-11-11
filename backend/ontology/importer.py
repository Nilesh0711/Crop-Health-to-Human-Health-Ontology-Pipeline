"""Explicit, attributed schema extension from a local RDF/OWL module."""
import hashlib
import json
from pathlib import Path

from rdflib import URIRef
from rdflib.namespace import RDF, OWL, RDFS

from backend.ontology.catalog import RESOURCE_DIR, rdf_graph, Catalog, DEFINITION
from backend.shared.artifacts import save_json
from backend.shared.storage import utc_now


def import_module(path, selected, note, *, format='turtle', directory=RESOURCE_DIR):
    if not note.strip() or not selected:
        raise ValueError('Provide explicit term identifiers and a schema-selection rationale')
    directory, path = Path(directory), Path(path)
    data = path.read_bytes()
    graph = rdf_graph(data, format)
    declared = {str(s): o for s, o in graph.subject_objects(RDF.type) if isinstance(s, URIRef)
                and o in {OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty}}
    unknown = set(selected) - declared.keys()
    if unknown:
        raise ValueError('Requested terms are not declared in the supplied module: ' + ', '.join(sorted(unknown)))
    current = Catalog(directory)
    for iri in selected:
        if iri in current.terms:
            for predicate in (RDFS.label, DEFINITION):
                before = {str(v) for v in current.graph.objects(URIRef(iri), predicate)}
                after = {str(v) for v in graph.objects(URIRef(iri), predicate)}
                if before and after and before != after:
                    raise ValueError('Existing identifier has a different definition/label; review a reference-version replacement instead of silently merging it')
    digest = hashlib.sha256(data).hexdigest()
    name = 'fragments/import_' + digest[:24] + {'xml': '.rdf', 'turtle': '.ttl', 'nt': '.nt'}[format]
    (directory / 'fragments').mkdir(parents=True, exist_ok=True)
    (directory / name).write_bytes(data)
    manifest = json.loads((directory / 'manifest.json').read_text(encoding='utf-8'))
    if not any(m['sha256'] == digest for m in manifest['modules']):
        manifest['modules'].append({'file': name, 'format': format, 'sha256': digest,
            'source': str(path.name), 'retrieved_at': utc_now(), 'selection_note': note,
            'term_iris': sorted(declared), 'scope': 'explicit local import; ontology release is not inferred'})
    profile = json.loads((directory / 'profile.json').read_text(encoding='utf-8'))
    for iri in selected:
        key = {OWL.Class: 'allowed_classes', OWL.ObjectProperty: 'allowed_object_properties',
               OWL.DatatypeProperty: 'allowed_data_properties'}[declared[iri]]
        profile[key] = sorted(set(profile.get(key, [])) | {iri})
    profile['version'] += 1
    profile.setdefault('selection_history', []).append({'at': utc_now(), 'terms': selected, 'note': note})
    save_json(directory / 'manifest.json', manifest)
    save_json(directory / 'profile.json', profile)
    return {'imported': len(selected), 'source_sha256': digest}
