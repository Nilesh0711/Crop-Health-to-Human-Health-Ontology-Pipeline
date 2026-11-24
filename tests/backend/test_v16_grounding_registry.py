"""Source annotation, admission and display contracts; no paid model calls."""
from copy import deepcopy
import json
from pathlib import Path
from threading import RLock
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from rdflib import Graph, URIRef
from rdflib.namespace import RDF, RDFS

from backend.domain.extraction_review import assertion_eligible, extraction_warnings
from backend.ontology.catalog import Catalog, RESOURCE_DIR
from backend.ontology.registry import property_relations, extraction_inventory
from backend.pipelines.ontology import rebuild, export_rdf, current_assertions
from backend.shared.artifacts import save_rows
from backend.shared.ontology_browser import create_router
from backend.stages.ontology_grounding.contract import check_response
from backend.stages.ontology_grounding.grounder import request_packet, ground_claims
from backend.stages.ontology_grounding.rows import parse_rows
from backend.stages.ontology_reasoning.engine import reason
from backend.viewer.static_export import _catalog_export
from tests.backend.test_ontology_grounding import Grounder, claim
from tests.backend.test_v15_classification import BroaderGrounder, source_claim

BASE = 'http://purl.obolibrary.org/obo/'


@pytest.fixture(scope='module')
def reference():
    return Catalog(RESOURCE_DIR)


def test_annotations_keep_separate_verbatim_text_and_provenance(reference):
    term = reference.terms[BASE + 'RO_0002304']
    assert 'execution of p is required' in term['definition']
    assert 'progression of x increases' in term['comments'][0]
    assert 'progression of x increases' not in term['definition']
    for a in term['annotation_provenance']:
        assert a['source'] and len(a['sha256']) == 64
    root = reference.terms[BASE + 'RO_0002410']
    assert root['allowed_mappings'] == ['broader', 'unresolved'] and not root['direct_use_allowed']
    assert root['usage_notes'][0] in [str(x) for x in reference.graph.objects(URIRef(root['iri']), URIRef(BASE + 'IAO_0000232'))]
    exported = _catalog_export(reference, [])['terms'][term['iri']]
    assert exported['comments'] == term['comments']
    assert exported['annotation_provenance'] == term['annotation_provenance']


def test_usage_metadata_must_be_supported_by_imported_annotation(monkeypatch):
    from backend.ontology.usage import usage_metadata
    metadata = deepcopy(usage_metadata())
    metadata['restrictions'][BASE + 'RO_0002410']['annotation_value'] = 'Invented rule.'
    monkeypatch.setattr('backend.ontology.catalog.usage_metadata', lambda: metadata)
    with pytest.raises(ValueError, match='no matching upstream annotation'):
        Catalog(RESOURCE_DIR)


def test_comment_does_not_become_a_missing_definition(tmp_path):
    import hashlib
    graph=Graph(); iri=URIRef(BASE+'BFO_0000050')
    from rdflib import Literal
    from rdflib.namespace import OWL
    graph.add((iri,RDF.type,OWL.ObjectProperty))
    graph.add((iri,RDFS.label,Literal('part of')))
    graph.add((iri,RDFS.comment,Literal('A comment-only synthetic fixture.')))
    raw=graph.serialize(format='turtle').encode()
    (tmp_path/'module.ttl').write_bytes(raw)
    (tmp_path/'profile.json').write_text(json.dumps({'allowed_classes':[], 'allowed_object_properties':[str(iri)]}))
    (tmp_path/'manifest.json').write_text(json.dumps({'modules':[{'file':'module.ttl','format':'turtle',
        'sha256':hashlib.sha256(raw).hexdigest(),'ontology':'BFO','term_iris':[str(iri)],'source':'https://example.test/fixture'}]}))
    term=Catalog(tmp_path).terms[str(iri)]
    assert not term['definition'] and term['comments']==['A comment-only synthetic fixture.']


@pytest.mark.parametrize('mapping', ['exact', 'broader', 'unresolved'])
@pytest.mark.parametrize('format', ['json', 'tsv'])
def test_both_formats_enforce_upstream_property_use(reference, mapping, format):
    payload, _ = request_packet([source_claim()], reference)
    answer = Grounder().chat_json('', json.dumps(payload), response_check=lambda _: None)
    alias = next(k for k,t in payload['relations'].items() if t['label']=='causally related to')
    signed = next(t for t in payload['relations'].values() if t['label']=='causally upstream of, positive effect')
    assert signed['comments'] and 'required for' in signed['definition']
    answer['results'][0]['relation'].update(mapping=mapping, term=None if mapping=='unresolved' else alias)
    def check():
        if format=='json':
            return check_response(answer, payload)
        lines=['C1.scope\tscope\tin_scope\t-\tFixture scope.']
        for role in ['subject','object','relation']:
            b=answer['results'][0][role]
            lines.append('\t'.join(['C1.'+role, 'relation' if role=='relation' else 'concept',b['mapping'],b['term'] or '-',b['reason']]))
        return parse_rows('\n'.join(lines),payload)
    if mapping=='exact':
        with pytest.raises(ValueError): check()
    else:
        assert check()


def test_restricted_legacy_predicate_never_admitted_or_exported(tmp_path, reference):
    records=ground_claims([source_claim()],tmp_path,reference,BroaderGrounder())
    row=records[0]; row['bindings']['relation'].update(mapping='exact',iri=BASE+'RO_0002410')
    row['assembly_eligible']=True
    assert not assertion_eligible(row)
    export_rdf(tmp_path/'out.ttl',reference,records)
    assert not list(Graph().parse(tmp_path/'out.ttl').triples((None,RDF.predicate,None)))
    minimal=SimpleNamespace(graph=Graph(),profile=reference.profile,manifest={},signature=reference.signature,rules_signature=reference.rules_signature)
    assert reason(minimal,records)['coverage']['skipped_assertions']


def test_manual_exact_restriction_and_stale_results(tmp_path,reference,monkeypatch):
    output=tmp_path/'paper';output.mkdir()
    save_rows(output/'candidates.jsonl',[source_claim()])
    rows=ground_claims([source_claim()],output,reference,BroaderGrounder())
    rebuild(tmp_path,reference)
    monkeypatch.setattr('backend.shared.ontology_browser.catalog',lambda:reference)
    workspace=SimpleNamespace(ONTOLOGY_DIR=tmp_path,_lock=RLock(),_jobs={},_pdf_path=lambda _:tmp_path/'paper.pdf',_output_dir=lambda _:output,rebuild_ontology=rebuild)
    app=FastAPI();app.include_router(create_router(workspace))
    with TestClient(app) as client:
        row=client.get('/api/ontology/assertions').json()['assertions'][0]
        payload={'role':'relation','mapping':'exact','iri':BASE+'RO_0002410','note':'Reviewed against upstream guidance.','expected_revision':row['revision']}
        assert client.post('/api/ontology/pdfs/p/claims/one/review',json=payload).status_code==422
        payload['mapping']='broader'
        assert client.post('/api/ontology/pdfs/p/claims/one/review',json=payload).status_code==200
    rows[0]['policy']='established-only-context-grounding-v10'
    save_rows(output/'ontology_grounding.jsonl',rows)
    assert not current_assertions(tmp_path,reference)[0]


def test_later_result_clauses_get_mechanism_holds_without_rewriting():
    source='Group A allocated Compound Q to Organ O, thereby enhancing Trait R and facilitating increased Trait S.'
    for relation,outcome in [('enhancing','Trait R'),('facilitating increased','Trait S')]:
        c={'subject':'Group A','subject_source_mention':'Group A','relation':relation,'object':outcome,
           'qualifiers':{'population':'Group A'},'source_evidence_span':source}
        before=deepcopy(c)
        assert 'possible_mechanism_substitution' in {w['code'] for w in extraction_warnings(c)}
        assert c==before
    control={'subject':'Treatment T','relation':'increased','object':'Trait R','qualifiers':{},
             'source_evidence_span':'Treatment T increased Trait R.'}
    assert not extraction_warnings(control)
    ordinary={'subject':'Group A','relation':'demonstrated enhanced','object':'responsiveness',
              'qualifiers':{'population':'Group A'},
              'source_evidence_span':'Group A demonstrated a notable increase in Trait Q and enhanced responsiveness.'}
    assert not extraction_warnings(ordinary)


def registry_fixture():
    f=json.loads(Path('tests/fixtures/relation_registry_v16.json').read_text())
    ref=SimpleNamespace(terms=f['terms'],profile={'allowed_object_properties':list(f['terms'])},canonical_equivalent=lambda x:x)
    rows=[]
    for c in f['cases']:
        rows.append({'candidate_id':c['id'],'document_key':c['document'],'scope':c['scope'],
                     'original_claim':{'subject':'Entity Q','relation':c['relation'],'object':'Entity R','qualifiers':{}},
                     'bindings':{'relation':{'iri':c['iri'],'mapping':c['mapping'],'reason':'Fixture mapping.'}}})
    inventory=[{'candidate_id':r['candidate_id'],'document_key':r['document_key']} for r in rows]+[f['pending']]
    return rows,ref,inventory


def test_grouping_counts_claims_not_rows_and_preserves_decisions():
    rows,ref,inventory=registry_fixture()
    result=property_relations(rows,ref,inventory=inventory)
    assert result['mention_counts']=={'all':5,'grounded':2,'classification':1,'classified':3,'unresolved':2}
    assert result['total']==1 and result['represented_claims']==3
    assert result['extracted_claim_relations']==8 and result['awaiting_grounding_claims']==1
    group=result['relations'][0]
    assert (group['exact_count'],group['broader_count'])==(2,1)
    assert {o['mapping'] for o in group['occurrences']}=={'exact','broader'}
    un=property_relations(rows,ref,status='unresolved',inventory=inventory)
    assert un['total']==1 and un['represented_claims']==2
    searched=property_relations(rows,ref,q='directly',inventory=inventory)
    assert searched['mention_counts']['classified']==1 and searched['relations'][0]['exact_count']==0
    assert property_relations(rows,ref,status='grounded')['represented_claims']==2
    assert property_relations(rows,ref,scope='all')['mention_counts']['all']==7
    assert property_relations(rows,ref,document='Paper B')['mention_counts']['all']==2
    assert property_relations(rows,ref,offset=1,limit=1)['relations']==[]
    assert property_relations(rows,ref)['extracted_claim_relations'] is None


def test_inventory_excludes_superseded_and_invalid_claims(tmp_path):
    assert extraction_inventory(tmp_path) is None
    folder=tmp_path/'paper';folder.mkdir()
    save_rows(folder/'candidates.jsonl',[{'candidate_id':'a'},{'candidate_id':'b','superseded_by':'a'}, {'candidate_id':'c','schema_error':'bad'}])
    assert extraction_inventory(tmp_path)==[{'document_key':'paper','candidate_id':'a'}]


def test_fixed_controls_and_no_runtime_case_specific_rules():
    from backend.config.pipeline import POST_GROUNDING_STAGES_PAUSED
    from backend.stages.claim_extraction.prompt import build_extraction_prompt
    from backend.stages.ontology_grounding.contract import GROUNDING_INSTRUCTIONS
    controls=json.loads(Path('tests/fixtures/atomic_grounding_v16_controls.json').read_text())
    assert len(controls['cases'])==12 and controls['review_status']=='pending'
    prompt=build_extraction_prompt(0,'target_context')
    assert all(x in prompt for x in ['ability','joint','destination','mechanism','declined'])
    assert 'silently replace or weaken' in GROUNDING_INSTRUCTIONS
    assert 'TO:0000387' not in GROUNDING_INSTRUCTIONS
    assert 'Milva' not in prompt and 'Lady Claire' not in prompt
    assert POST_GROUNDING_STAGES_PAUSED
