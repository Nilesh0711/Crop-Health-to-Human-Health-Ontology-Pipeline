"""Explicit acquisition of small ontology fragments from Ontobee.

No request is made while running extraction or browsing. Each response is kept
verbatim and hashed. These are bounded term fragments, not a complete OWL module.
"""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from urllib.parse import urlencode
import json
import hashlib

import httpx
from rdflib import URIRef
from rdflib.namespace import RDF, RDFS, OWL

from backend.ontology.catalog import RESOURCE_DIR, rdf_graph, LOCAL
from backend.shared.artifacts import save_json
from backend.shared.storage import utc_now

SEEDS = {
    'CHEBI': [
        'CHEBI_17632', 'CHEBI_28938', 'CHEBI_26020', 'CHEBI_33284', 'CHEBI_18367',
        'CHEBI_29103', 'CHEBI_28659', 'CHEBI_43474', 'CHEBI_39745', 'CHEBI_16411',
        'CHEBI_17992', 'CHEBI_28017', 'CHEBI_16646', 'CHEBI_16134', 'CHEBI_22676',
        'CHEBI_25555', 'CHEBI_26078', 'CHEBI_26216', 'CHEBI_28057', 'CHEBI_28102',
        'CHEBI_33461', 'CHEBI_33937', 'CHEBI_35780', 'CHEBI_37848', 'CHEBI_76924',
        'CHEBI_33287', 'CHEBI_25212', 'CHEBI_25710', 'CHEBI_36080',
    ],
    'ENVO': [
        'ENVO_00001998', 'ENVO_00005801', 'ENVO_00002982', 'ENVO_00010483',
        'ENVO_01000016', 'ENVO_01000017', 'ENVO_01000155', 'ENVO_06105304',
        'ENVO_00005754', 'ENVO_01000999', 'ENVO_09000030', 'ENVO_09200010',
    ],
    'PO': [
        'PO_0009005', 'PO_0025522', 'PO_0004543', 'PO_0025034', 'PO_0025131',
        'PO_0025025', 'PO_0009006', 'PO_0020121', 'PO_0009008', 'PO_0009011',
        'PO_0025007', 'PO_0025029', 'PO_0025073', 'PO_0003024', 'PO_0000256',
        'PO_0000025', 'PO_0005352', 'PO_0005417', 'PO_0009015',
    ],
    'TO': [
        'TO_0020093', 'TO_0000227', 'TO_0001024', 'TO_0000609', 'TO_0000696',
        'TO_0000078', 'TO_0000552', 'TO_0000043', 'TO_0000985', 'TO_0000511',
        'TO_0006045', 'TO_0000306', 'TO_0000084', 'TO_0000233', 'TO_0000654',
        'TO_0000112', 'TO_0000333', 'TO_0000340', 'TO_0000278', 'TO_0000457',
        'TO_0000277', 'TO_0000279', 'TO_0000281', 'TO_0000291', 'TO_0000352',
        'TO_0000371', 'TO_0000387', 'TO_0000398', 'TO_0000465', 'TO_0000568',
        'TO_0000576', 'TO_0000736', 'TO_0000738', 'TO_0000842', 'TO_0000928',
        'TO_0006007', 'TO_0020096', 'TO_0000011', 'TO_0000102', 'TO_0000008',
        'TO_0000248', 'TO_0000480', 'TO_0000168', 'TO_0000514', 'TO_0020100',
        'TO_0000506', 'TO_0020094', 'TO_0020102', 'TO_0001124', 'TO_0003081',
        'TO_0003067', 'TO_0020082', 'TO_0000328', 'TO_0020106', 'TO_0002672',
        'TO_0000704', 'TO_0000327', 'TO_0000571', 'TO_0000578', 'TO_0000143',
        'TO_0001019', 'TO_0000128', 'TO_0001071', 'TO_0003012', 'TO_0003013',
        'TO_0001012', 'TO_0020103', 'TO_0002665', 'TO_0001051', 'TO_0001056',
        'TO_0000115', 'TO_0000257', 'TO_0000515', 'TO_0000368', 'TO_0000360',
        'TO_0000250', 'TO_0000207', 'TO_0000442', 'TO_1000043',
    ],
    'GO': [
        'GO_0048364', 'GO_0015706', 'GO_0072488', 'GO_0006817', 'GO_0006813',
        'GO_0042128', 'GO_0035618', 'GO_0003993', 'GO_0006810', 'GO_0022622',
        'GO_0055085', 'GO_0099402', 'GO_0016791', 'GO_0010467', 'GO_0009684',
        'GO_0009926', 'GO_0005315', 'GO_0015415', 'GO_0010966', 'GO_0015112',
        'GO_0008519', 'GO_0110067', 'GO_0071705', 'GO_0015770', 'GO_0008643',
        'GO_0055062', 'GO_0016036', 'GO_0006995', 'GO_0090548', 'GO_0010167',
        'GO_0031667', 'GO_0040007', 'GO_0048527', 'GO_0048767', 'GO_0010449',
        'GO_0048367', 'GO_0010016', 'GO_0015977', 'GO_0044403',
    ],
    'NCBITaxon': ['NCBITaxon_4113', 'NCBITaxon_1423', 'NCBITaxon_2', 'NCBITaxon_4751'],
    'PECO': [
        'PECO_0007246', 'PECO_0007233', 'PECO_0007397', 'PECO_0001046', 'PECO_0001006',
        'PECO_0001001', 'PECO_0007240', 'PECO_0007357', 'PECO_0001049',
        'PECO_0007102', 'PECO_0007293', 'PECO_0001051', 'PECO_0007104', 'PECO_0007087',
        'PECO_0007147', 'PECO_0007067',
    ],
    'PATO': [
        'PATO_0000033', 'PATO_0001323', 'PATO_0000122', 'PATO_0000070', 'PATO_0000117',
        'PATO_0000125', 'PATO_0000141', 'PATO_0000915', 'PATO_0000918', 'PATO_0001470',
        'PATO_0001555',
    ],
    'RO': [
        'BFO_0000050', 'BFO_0000051', 'RO_0000086', 'RO_0000052', 'RO_0001025',
        'RO_0002162', 'RO_0000056', 'RO_0000057', 'RO_0002020', 'RO_0002233',
        'RO_0002234', 'RO_0002411', 'RO_0002304', 'RO_0002305', 'RO_0002211', 'RO_0002213',
        'RO_0002212', 'RO_0002410', 'RO_0003000', 'RO_0002610',
    ],
    'PCO': ['PCO_1000004'],
    'SO': ['SO_0000704'],
}


def acquire(directory=RESOURCE_DIR, *, max_depth=2, max_terms=650, refresh=False):
    """Fetch selected terms plus bounded named ancestors; never claim completeness."""
    directory = Path(directory)
    fragments = directory / 'fragments'
    fragments.mkdir(parents=True, exist_ok=True)
    seen, sources, failures = set(), [], []
    pending = [(ontology, 'http://purl.obolibrary.org/obo/' + term) for ontology, terms in SEEDS.items() for term in terms]

    def fetch(item):
        ontology, iri = item
        url = 'https://ontobee.org/ontology/rdf/' + ontology + '?' + urlencode({'iri': iri})
        filename = 'fragments/' + ontology + '_' + iri.rsplit('/', 1)[-1] + '.rdf'
        path = directory / filename
        if path.exists() and not refresh:
            raw = path.read_bytes()
        else:
            with httpx.Client(timeout=40, follow_redirects=True) as client:
                response = client.get(url)
                response.raise_for_status()
            raw = response.content
            path.write_bytes(raw)
        graph = rdf_graph(raw, 'xml')
        term = URIRef(iri)
        if not any(graph.triples((term, None, None))):
            raise ValueError(f'No definition was returned for {iri}')
        parents = [str(v) for predicate in (RDFS.subClassOf, RDFS.subPropertyOf)
                   for v in graph.objects(term, predicate) if isinstance(v, URIRef)]
        declared = sorted({str(s) for s, _, o in graph.triples((None, RDF.type, None))
                           if isinstance(s, URIRef) and o in {OWL.Class, OWL.ObjectProperty, OWL.DatatypeProperty}})
        return {'file': filename, 'format': 'xml', 'source': url, 'ontology': ontology,
                'sha256': hashlib.sha256(raw).hexdigest(), 'retrieved_at': utc_now(),
                'version_policy': 'immutable retrieved term fragment identified by SHA-256; upstream release not asserted',
                'term_iris': declared, 'requested_iri': iri}, parents

    for depth in range(max_depth + 1):
        pending = sorted(set(pending) - seen)
        if len(seen) + len(pending) > max_terms:
            raise ValueError('Reference import budget exceeded; narrow the reviewed seed set')
        seen.update(pending)
        next_terms = []
        with ThreadPoolExecutor(max_workers=4) as pool:
            futures = [(item, pool.submit(fetch, item)) for item in pending]
            for item, future in futures:
                try:
                    source, parents = future.result()
                    sources.append(source)
                    if depth < max_depth:
                        for iri in parents:
                            if not iri.startswith('http://purl.obolibrary.org/obo/'):
                                continue
                            prefix = iri.rsplit('/', 1)[-1].split('_', 1)[0]
                            ontology = prefix if prefix in SEEDS else item[0]
                            next_terms.append((ontology, iri))
                except Exception as exc:
                    failures.append({'ontology': item[0], 'iri': item[1], 'error': str(exc)})
        pending = next_terms
    if any(f['iri'] in {'http://purl.obolibrary.org/obo/' + t for values in SEEDS.values() for t in values} for f in failures):
        save_json(directory / 'import_failures.json', failures)
        raise RuntimeError('Some required seed terms failed to import; inspect import_failures.json')
    local_path = directory / 'application.ttl'
    local_graph = rdf_graph(local_path.read_bytes(), 'turtle')
    sources.append({'file': 'application.ttl', 'format': 'turtle', 'source': 'Project application schema',
                    'sha256': hashlib.sha256(local_path.read_bytes()).hexdigest(), 'retrieved_at': utc_now(),
                    'term_iris': sorted(str(s) for s in local_graph.subjects(RDF.type, None) if isinstance(s, URIRef))})
    manifest = {'format_version': 1, 'created_at': utc_now(), 'modules': sources,
                'coverage': 'Selected term fragments and bounded ancestors; full import closure and full OWL reasoning are not claimed',
                'ancestor_depth': max_depth, 'unavailable_supporting_terms': failures}
    save_json(directory / 'manifest.json', manifest)
    profile = json.loads((directory / 'profile.json').read_text(encoding='utf-8'))
    declared = {i for source in sources for i in source['term_iris']}
    unknown = set(profile['allowed_classes'] + profile['allowed_object_properties']) - declared
    if unknown:
        raise ValueError('Approved selection contains undeclared identifiers: ' + ', '.join(sorted(unknown)))
    return {'terms_requested': len(seen), 'fragments': len(sources), 'supporting_failures': failures}
