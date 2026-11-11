"""Read actual RDF/OWL definitions; never manufacture an external identifier.

Imports are frozen, local and content-addressed. owl:imports is recorded, not
followed over the network. The reasoner reports its supported axiom fragment.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from collections import defaultdict
from hashlib import sha256
from threading import RLock

from rdflib import Graph, URIRef, Literal
from rdflib.namespace import RDF, RDFS, OWL, XSD

from backend.config.paths import BACKEND_DIR
from backend.shared.storage import stable_hash

RESOURCE_DIR = BACKEND_DIR / 'resources' / 'ontologies'
LOCAL = 'urn:soil-crop-health:'
DEFINITION = URIRef('http://purl.obolibrary.org/obo/IAO_0000115')
SYNONYMS = [URIRef('http://www.geneontology.org/formats/oboInOwl#' + name)
            for name in ('hasExactSynonym', 'hasRelatedSynonym', 'hasBroadSynonym', 'hasNarrowSynonym')]
KINDS = {OWL.Class: 'class', RDFS.Class: 'class', OWL.ObjectProperty: 'object_property',
         OWL.DatatypeProperty: 'data_property', OWL.AnnotationProperty: 'annotation_property'}
_lock, _cache = RLock(), {}


def rdf_graph(data: bytes, format: str):
    if len(data) > 30 * 1024 * 1024:
        raise ValueError('Ontology import exceeds the 30 MB reviewed-module limit')
    if format not in {'xml', 'turtle', 'nt'}:
        raise ValueError('Use RDF/XML, Turtle or N-Triples; remote contexts/imports are not followed')
    if b'<!DOCTYPE' in data.upper() or b'<!ENTITY' in data.upper():
        raise ValueError('Ontology XML must not contain a DTD or entity declarations')
    return Graph().parse(data=data, format=format)


def _literal(graph, subject, predicate):
    values = list(graph.objects(subject, predicate))
    preferred = [v for v in values if isinstance(v, Literal) and v.language in (None, 'en')]
    return str((preferred or values or [''])[0])


def _clean_synonym(value):
    text = re.sub(r'\s*\((?:[^()]*,\s*)?(?:exact|related|broad|narrow)\)\s*$', '', str(value), flags=re.IGNORECASE).strip()
    return text or str(value).strip()


class Catalog:
    def __init__(self, directory=RESOURCE_DIR):
        self.directory = Path(directory)
        profile_path = self.directory / 'profile.json'
        self.profile = json.loads(profile_path.read_text(encoding='utf-8'))
        self.manifest = json.loads((self.directory / 'manifest.json').read_text(encoding='utf-8'))
        self.graph = Graph()
        preferred = {}
        self.axiom_sources = defaultdict(list)
        fingerprints = []
        for source in self.manifest['modules']:
            path = (self.directory / source['file']).resolve()
            if not path.is_relative_to(self.directory.resolve()):
                raise ValueError('Ontology module must be inside the reference directory')
            data = path.read_bytes()
            digest = sha256(data).hexdigest()
            if digest != source['sha256']:
                raise ValueError(f'Ontology module hash changed: {source["file"]}; reimport it explicitly')
            parsed = rdf_graph(data, source['format'])
            if not source.get('ontology') and any(str(s).startswith('http://purl.obolibrary.org/obo/') for s in parsed.subjects()):
                raise ValueError('Application modules may not override upstream scientific annotations')
            if source.get('requested_iri'):
                iri = source['requested_iri']
                uri = URIRef(iri)
                preferred[iri] = {'label': _literal(parsed, uri, RDFS.label),
                    'definition': _literal(parsed, uri, DEFINITION) or _literal(parsed, uri, RDFS.comment)}
            for s, p, o in parsed:
                if isinstance(s, URIRef) and isinstance(o, URIRef):
                    self.axiom_sources[(str(s), str(p), str(o))].append({'source': source['source'], 'sha256': digest})
            self.graph += parsed
            fingerprints.append(digest)
        mapping_profile = {k: v for k, v in self.profile.items() if k != 'biological_rules'}
        self.signature = stable_hash(json.dumps({'modules': fingerprints, 'profile': mapping_profile, 'annotation_policy': 'upstream-only-v1'}, sort_keys=True), 64)
        self.rules_signature = stable_hash(json.dumps(self.profile.get('biological_rules', []), sort_keys=True), 64)
        self.terms = {}
        for uri, rdf_type in self.graph.subject_objects(RDF.type):
            if not isinstance(uri, URIRef) or rdf_type not in KINDS:
                continue
            iri = str(uri)
            kind = KINDS[rdf_type]
            if iri in self.terms and kind == 'annotation_property':
                continue
            parent_predicate = RDFS.subClassOf if kind == 'class' else RDFS.subPropertyOf
            parents = sorted(str(v) for v in self.graph.objects(uri, parent_predicate) if isinstance(v, URIRef))
            self.terms[iri] = {'iri': iri, 'label': preferred.get(iri, {}).get('label') or _literal(self.graph, uri, RDFS.label) or iri.rsplit('/', 1)[-1],
                'id': iri.rsplit('/', 1)[-1].replace('_', ':') if '/obo/' in iri else iri,
                'kind': kind, 'definition': preferred.get(iri, {}).get('definition') or _literal(self.graph, uri, DEFINITION) or _literal(self.graph, uri, RDFS.comment),
                'synonyms': sorted({_clean_synonym(v) for predicate in SYNONYMS for v in self.graph.objects(uri, predicate) if _clean_synonym(v)}),
                'exact_synonyms': sorted({_clean_synonym(v) for v in self.graph.objects(uri, SYNONYMS[0]) if _clean_synonym(v)}),
                'related_synonyms': sorted({_clean_synonym(v) for v in self.graph.objects(uri, SYNONYMS[1]) if _clean_synonym(v)}),
                'broad_synonyms': sorted({_clean_synonym(v) for v in self.graph.objects(uri, SYNONYMS[2]) if _clean_synonym(v)}),
                'narrow_synonyms': sorted({_clean_synonym(v) for v in self.graph.objects(uri, SYNONYMS[3]) if _clean_synonym(v)}),
                'parents': parents, 'domain': sorted(str(v) for v in self.graph.objects(uri, RDFS.domain)),
                'range': sorted(str(v) for v in self.graph.objects(uri, RDFS.range)),
                'deprecated': any(str(v).lower() == 'true' for v in self.graph.objects(uri, OWL.deprecated)),
                'origin': 'project_schema' if iri.startswith(LOCAL) else 'established_ontology'
                    if any(m.get('ontology') and iri in m.get('term_iris', []) for m in self.manifest['modules'])
                    else 'imported_reference_module',
                'selectable': iri in self.profile['allowed_classes'] or iri in self.profile['allowed_object_properties']
                              or iri in self.profile.get('allowed_data_properties', []),
                'sources': [m['source'] for m in self.manifest['modules'] if iri in m.get('term_iris', [])]}
        for iri in self.profile['allowed_classes'] + self.profile['allowed_object_properties']:
            term = self.terms.get(iri)
            if (not iri.startswith('http://purl.obolibrary.org/obo/') or term is None
                    or term['origin'] != 'established_ontology' or term['deprecated']):
                raise ValueError(f'Selectable scientific term must be an established, non-deprecated ontology term: {iri}')
        self.children = defaultdict(set)
        for term in self.terms.values():
            for parent in term['parents']:
                self.children[parent].add(term['iri'])
        self.selectable_counts = {
            'class': sum(1 for t in self.terms.values() if t.get('kind') == 'class' and t.get('selectable') and not t.get('deprecated')),
            'object_property': sum(1 for t in self.terms.values() if t.get('kind') == 'object_property' and t.get('selectable') and not t.get('deprecated')),
            'data_property': sum(1 for t in self.terms.values() if t.get('kind') == 'data_property' and t.get('selectable') and not t.get('deprecated')),
        }
        self.all_counts = {
            'class': sum(1 for t in self.terms.values() if t.get('kind') == 'class' and not t.get('deprecated')),
            'object_property': sum(1 for t in self.terms.values() if t.get('kind') == 'object_property' and not t.get('deprecated')),
            'data_property': sum(1 for t in self.terms.values() if t.get('kind') == 'data_property' and not t.get('deprecated')),
        }

    def search(self, query='', kind=None, parent=None, limit=50, offset=0, selectable=False):
        needle = query.casefold().strip()
        rows = [t for t in self.terms.values() if (not kind or t['kind'] == kind)
                and (not parent or t['iri'] in self.children[parent])
                and (not selectable or t['selectable']) and not t['deprecated']
                and (not needle or needle in ' '.join([t['label'], t['id'], t['iri'], *t['synonyms']]).casefold())]
        rows.sort(key=lambda t: (needle not in {t['label'].casefold(), t['id'].casefold()}, t['label'].casefold(), t['iri']))
        return {'terms': [self.brief(t['iri']) for t in rows[offset:offset + limit]],
                'total': len(rows), 'offset': offset, 'limit': limit}

    def brief(self, iri):
        term = self.terms[iri]
        return {**term, 'child_count': len(self.children[iri]), 'has_children': bool(self.children[iri])}

    def ancestors(self, iri):
        seen, pending = set(), list(self.terms.get(iri, {}).get('parents', []))
        while pending:
            parent = pending.pop()
            if parent in seen:
                continue
            seen.add(parent)
            pending.extend(self.terms.get(parent, {}).get('parents', []))
        return seen

    def incompatible(self, left, right):
        """Only declared disjointness; different labels/types do not prove disjointness."""
        left_types, right_types = {left} | self.ancestors(left), {right} | self.ancestors(right)
        return any((URIRef(a), OWL.disjointWith, URIRef(b)) in self.graph
                   or (URIRef(b), OWL.disjointWith, URIRef(a)) in self.graph
                   for a in left_types for b in right_types)

    def canonical_equivalent(self, iri):
        """Only explicit named OWL equivalence; hierarchy/role similarity is not identity."""
        term = self.terms.get(iri, {})
        predicate = OWL.equivalentClass if term.get('kind') == 'class' else OWL.equivalentProperty
        seen, pending = {iri}, [iri]
        while pending:
            current = URIRef(pending.pop())
            peers = [*self.graph.objects(current, predicate), *self.graph.subjects(predicate, current)]
            for peer in peers:
                value = str(peer)
                if (isinstance(peer, URIRef) and value not in seen and value in self.terms
                        and self.terms[value]['kind'] == term.get('kind') and not self.incompatible(iri, value)):
                    seen.add(value); pending.append(value)
        approved = [value for value in seen if self.terms.get(value, {}).get('selectable')]
        return min(approved or [iri])

    def extraction_instructions(self):
        # Extraction is ontology-agnostic: the ontology vocabulary belongs to grounding only.
        return ('ACTIVE APPLICATION SCOPE: ' + self.profile['research_question'] + '\n'
                + self.profile['scope_description'])



def catalog(directory=RESOURCE_DIR):
    directory = Path(directory)
    stamp = tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size)
                  for p in sorted(directory.rglob('*')) if p.is_file())
    key = str(directory.resolve())
    with _lock:
        if key not in _cache or _cache[key][0] != stamp:
            _cache[key] = stamp, Catalog(directory)
        return _cache[key][1]
