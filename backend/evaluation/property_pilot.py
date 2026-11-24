"""Explicit, isolated property acquisition. Normal pipeline execution is offline."""
from copy import deepcopy
import hashlib
import json
from pathlib import Path
import shutil
from urllib.parse import urlencode

from backend.config.paths import PROJECT_ROOT
from backend.ontology.catalog import Catalog, catalog, rdf_graph

PILOT_DIRECTORY = PROJECT_ROOT / 'analysis/acceptance_v18/property_catalogue_21'
ADDITIONS = ('http://purl.obolibrary.org/obo/RO_0002566', 'http://purl.obolibrary.org/obo/RO_0002559')


def prepare_pilot(destination=PILOT_DIRECTORY, *, reference=None, fetch=None):
    """Run explicitly during setup; no network from browsing or grounding."""
    from rdflib import URIRef
    from rdflib.namespace import RDF, OWL
    from backend.cli.repair_sample import safe_location
    import httpx
    reference = reference or catalog()
    destination = safe_location(destination)
    if destination.exists():
        raise ValueError('Pilot catalogue already exists; preserve it and choose a new output')
    profile, manifest = deepcopy(reference.profile), deepcopy(reference.manifest)
    # Acquire and verify first; a failed request must not leave a valid-looking catalogue.
    fragments = []
    for iri in ADDITIONS:
        url = 'https://ontobee.org/ontology/rdf/RO?' + urlencode({'iri': iri})
        if fetch:
            raw = fetch(url)
        else:
            response = httpx.get(url, timeout=45, follow_redirects=True)
            response.raise_for_status()
            raw = response.content
        graph = rdf_graph(raw, 'xml')
        if (URIRef(iri), RDF.type, OWL.ObjectProperty) not in graph:
            raise ValueError('Upstream fragment does not declare requested object property')
        if any(str(v).casefold() == 'true' for v in graph.objects(URIRef(iri), OWL.deprecated)):
            raise ValueError('Obsolete pilot properties are not selectable')
        fragments.append((iri, url, raw, graph))
    shutil.copytree(reference.directory, destination)
    for iri, url, raw, graph in fragments:
        filename = 'fragments/pilot_' + iri.rsplit('/', 1)[-1] + '.rdf'
        (destination / filename).write_bytes(raw)
        manifest['modules'].append({'ontology': 'RO', 'file': filename, 'source': url, 'format': 'xml',
            'requested_iri': iri, 'sha256': hashlib.sha256(raw).hexdigest(),
            'term_iris': sorted({str(s) for s in graph.subjects(RDF.type, None) if isinstance(s, URIRef)})})
        if iri not in profile['allowed_object_properties']:
            profile['allowed_object_properties'].append(iri)
    profile['version'] = 13
    profile['experiment_note'] = 'Isolated causal-influence property pilot; production catalogue unchanged.'
    (destination / 'profile.json').write_text(json.dumps(profile, indent=2), encoding='utf-8')
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    result = Catalog(destination)
    if result.profile['allowed_classes'] != reference.profile['allowed_classes']:
        raise ValueError('Pilot must preserve selectable classes exactly')
    return result


def pilot_catalog():
    if not (Path(PILOT_DIRECTORY) / 'profile.json').exists():
        raise ValueError('Prepare the isolated property catalogue explicitly before running its pilot')
    return catalog(PILOT_DIRECTORY)
