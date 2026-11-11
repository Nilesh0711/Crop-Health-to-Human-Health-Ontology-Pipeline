"""All approved terms reach the model without candidate retrieval or embeddings."""
from backend.config.settings import Settings
from backend.ontology.catalog import Catalog, RESOURCE_DIR
from backend.stages.ontology_grounding.grounder import request_packet, ground_claims, catalog_term_map, CLASS_SELECTION_POLICY
from tests.backend.test_ontology_grounding import claim, Grounder


def test_every_endpoint_receives_all_approved_classes_and_properties():
    reference = Catalog(RESOURCE_DIR)
    packet, _ = request_packet([claim('a'), claim('b', subject='unseen expression')], reference)
    expected = set(reference.profile['allowed_classes'])
    assert len(expected) == 211
    _, _, term_iris, _, relation_iris = catalog_term_map(reference)
    assert set(term_iris.values()) == expected
    assert set(relation_iris.values()) == set(reference.profile['allowed_object_properties'])
    assert set(packet['terms']) == set(term_iris)
    assert set(packet['relations']) == set(relation_iris)
    for source in packet['claims'].values():
        assert 'subject_candidates' not in source and 'object_candidates' not in source
    for term in packet['terms'].values():
        assert 'label' in term
        assert term.keys() <= {'label', 'definition', 'exact_synonyms', 'related_synonyms', 'parents'}
    for rel in packet['relations'].values():
        assert 'label' in rel
        assert rel.keys() <= {'label', 'definition', 'domain', 'range'}
    assert Settings().ontology_grounding_output_format == 'json'


def test_full_vocabulary_grounding_does_not_use_embeddings(tmp_path):
    class UnusedEncoder:
        def encode_texts(self, texts):
            raise AssertionError('Class shortlisting must not run')
    reference = Catalog(RESOURCE_DIR)
    results = ground_claims([claim()], tmp_path, reference, Grounder(), encoder=UnusedEncoder())
    assert results[0]['class_selection_policy'] == CLASS_SELECTION_POLICY
    assert results[0]['output_protocol'] == 'grounding-json-v1'
    assert len(results[0]['bindings']['subject']['candidate_iris']) == len(reference.profile['allowed_classes'])


def test_class_expansion_preserves_relation_scope_and_chemical_distinctions():
    from backend.ontology.bootstrap import SEEDS
    reference = Catalog(RESOURCE_DIR)
    assert reference.profile['version'] == 10
    assert set(reference.profile['allowed_object_properties']) == {'http://purl.obolibrary.org/obo/' + name for name in
         ('BFO_0000050', 'BFO_0000051', 'RO_0000086', 'RO_0000052', 'RO_0001025',
          'RO_0000056', 'RO_0000057', 'RO_0002020', 'RO_0002233', 'RO_0002234',
           'RO_0002411', 'RO_0002304', 'RO_0002305', 'RO_0002211', 'RO_0002213',
           'RO_0002212', 'RO_0002410', 'RO_0003000', 'RO_0002610')}
    assert len(reference.profile['allowed_data_properties']) == 9
    assert reference.profile['biological_rules'] == []
    for iri in reference.profile['allowed_classes']:
        term = reference.terms[iri]
        assert term['kind'] == 'class' and not term['deprecated'] and term['sources']
        if term['origin'] == 'established_ontology':
            # A later explicit sync must not drop a newly selectable class.
            assert iri.rsplit('/', 1)[-1] in {i for values in SEEDS.values() for i in values}
    # Adding a related chemical form must not collapse chemically distinct IDs.
    chemicals = ['CHEBI_28659', 'CHEBI_18367', 'CHEBI_43474', 'CHEBI_39745']
    assert len({reference.canonical_equivalent('http://purl.obolibrary.org/obo/' + i)
                for i in chemicals}) == 4


def test_deferred_classes_are_preserved_but_never_offered_for_grounding():
    reference = Catalog(RESOURCE_DIR)
    packet, _ = request_packet([claim()], reference)
    _, _, term_iris, _, _ = catalog_term_map(reference)
    offered = {term_iris[a] for a in packet['terms']}
    deferred = set(reference.profile.get('deferred_classes', []))
    assert not (offered & deferred)
    for iri in deferred:
        # Pausing a concept does not erase its original definition/provenance.
        assert iri in reference.terms and reference.terms[iri]['sources']
        assert not reference.terms[iri]['selectable']
    core = {'GO_0048364', 'GO_0035618', 'GO_0006817', 'GO_0006813',
            'TO_0000511', 'TO_0000078', 'TO_0006045', 'NCBITaxon_4113', 'GO_0003993'}
    assert {'http://purl.obolibrary.org/obo/' + i for i in core} <= offered
    assert packet['scope']['research_question'] == reference.profile['research_question']
    assert packet['scope']['scope_description'] == reference.profile['scope_description']
