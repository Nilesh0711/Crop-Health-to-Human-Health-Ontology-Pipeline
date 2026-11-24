"""Acceptance bookkeeping and source-bound safeguards; no live model calls."""
from copy import deepcopy
from pathlib import Path
import json

import pytest

from backend.domain.extraction_review import extraction_warnings
from backend.domain.treatment_labels import document_treatment_labels, labels_for_claim
from backend.evaluation.readiness import (output_record, evaluate_readiness, application_audit_plan,
    audit_review_status, verify_frozen_readiness, POLICY)
from backend.evaluation.property_pilot import prepare_pilot
from backend.ontology.catalog import catalog
from backend.shared.storage import file_hash
from backend.stages.ontology_grounding.grounder import request_packet


def sample_output(i, category='exact'):
    ref = catalog()
    root = next(t['iri'] for t in ref.terms.values() if t['label'] == 'root' and t['selectable'])
    binding = {'iri':root, 'mapping':'exact', 'denotation':'concept', 'reason':'Reviewed control.'}
    row = {'candidate_id':str(i), 'scope':'in_scope', 'bindings':{'subject':deepcopy(binding),'object':deepcopy(binding),
        'relation':{'iri':'http://purl.obolibrary.org/obo/BFO_0000050' if category!='unresolved' else None,
                    'mapping':category,'reason':'Control.'}},
        'original_claim':{'candidate_id':str(i),'subject':'root','relation':'part of','object':'root system','qualifiers':{},
            'source_target_id':'target','source_evidence_sentence_numbers':[1]}}
    return output_record('doc', row)


def review(output):
    return {'output_id':output['id'],'signature':output['signature'], 'atomicity':'atomic', 'meaning':'faithful',
        'mappings':{r:'justified' for r in ('subject','relation','object')},'disposition':'keep','scope_confirmed':True,
        'retained_findings':[],'note':'Reviewed the source and the complete supplied meanings.'}


def boundary_set():
    outputs = [sample_output(i) for i in range(10)]
    for o in outputs[4:]:
        o['row']['bindings']['object'].update(mapping='unresolved',iri=None)
        o.update(output_record('doc', o['row']))
    labels = [review(o) for o in outputs]
    labels[-1].update(atomicity='bundled',disposition='hold')
    controls = [{'id':'finding','document':'doc','target_ids':['target'],'sentence_ids':[1]}]
    labels[0]['retained_findings']=['finding']
    return outputs, labels, controls


def test_exact_thresholds_and_preserved_denominators():
    outputs, labels, controls = boundary_set()
    s=evaluate_readiness(outputs,labels,controls,catalog(),coverage_ok=True,cases_reviewed=True)
    assert s['ready'] and s['state']=='accepted_with_limitations'
    assert (s['classified_endpoints'],s['endpoint_mentions'],s['classified_claims'],s['claims'])==(14,20,4,10)
    labels[-2].update(atomicity='uncertain',disposition='reject')
    s=evaluate_readiness(outputs,labels,controls,catalog(),coverage_ok=True,cases_reviewed=True)
    assert not s['ready'] and s['non_atomic_rate']==.2 and s['claims']==10


def test_missing_findings_stale_reviews_and_serious_errors_block():
    outputs,labels,controls=boundary_set()
    labels[0]['retained_findings']=[]
    assert not evaluate_readiness(outputs,labels,controls,catalog(),coverage_ok=True,cases_reviewed=True)['ready']
    labels[0]['retained_findings']=['finding']
    labels[5]['meaning']='serious_error'
    assert evaluate_readiness(outputs,labels,controls,catalog(),coverage_ok=True,cases_reviewed=True)['serious_unheld']==1
    labels[5]['disposition']='hold'
    assert evaluate_readiness(outputs,labels,controls,catalog(),coverage_ok=True,cases_reviewed=True)['ready']
    labels[5]['signature']='changed'
    assert evaluate_readiness(outputs,labels,controls,catalog(),coverage_ok=True,cases_reviewed=True)['pending_reviews']==1


def test_corrected_formulations_and_empty_samples_cannot_pass():
    outputs,labels,controls=boundary_set()
    assert not evaluate_readiness(outputs,labels,controls,catalog(),coverage_ok=False,cases_reviewed=True)['ready']
    s=evaluate_readiness([],[],controls,catalog(),coverage_ok=True,cases_reviewed=True)
    assert not s['ready'] and s['entity_rate'] is None


def test_independent_endpoints_count_while_held_claim_does_not():
    o=sample_output(1);l=review(o);l.update(meaning='serious_error',disposition='hold')
    s=evaluate_readiness([o],[l],[],catalog(),coverage_ok=True,cases_reviewed=True,require_retention=False)
    assert s['classified_endpoints']==2 and s['classified_claims']==0 and not s['ready']


@pytest.mark.parametrize('status',['held','rejected'])
def test_saved_review_holds_cannot_complete_or_shrink_denominators(status):
    o=sample_output(1);o['row']['extraction_review_status']=status
    o=output_record('doc',o['row']);l=review(o)
    s=evaluate_readiness([o],[l],[],catalog(),coverage_ok=True,cases_reviewed=True,require_retention=False)
    assert s['claims']==1 and s['classified_endpoints']==2 and s['classified_claims']==0
    assert s['held']==1 and not s['ready']


def test_retention_does_not_erase_faithful_held_information():
    outputs,labels,controls=boundary_set()
    labels[0]['retained_findings']=[]
    labels[-1]['retained_findings']=['finding']
    s=evaluate_readiness(outputs,labels,controls,catalog(),coverage_ok=True,cases_reviewed=True)
    assert s['ready'] and s['retained_findings']==1 and s['non_atomic_claims']==1
    labels[-1]['meaning']='serious_error'
    assert not evaluate_readiness(outputs,labels,controls,catalog(),coverage_ok=True,cases_reviewed=True)['ready']


def test_source_defined_codes_and_foreign_provenance():
    packets=[{'packet_number':1,'sentences':[{'text':'Four nutrient levels were labelled as Q0.5, Q2, Q5 and Q30.','page':1}]}]
    labels=document_treatment_labels(packets,'document','pdf')
    assert {r['label'] for r in labels}=={'Q0.5','Q2','Q5','Q30'}
    c={'subject':'root length','relation':'was elevated to','object':'Q30','source_document_id':'document',
       'source_pdf_sha256':'pdf','source_evidence_span':'Root length was elevated to Q30.'}
    c['source_treatment_labels']=labels_for_claim(c,labels)
    warnings=extraction_warnings(c)
    assert any(w['code']=='possible_treatment_code_endpoint' for w in warnings)
    assert c['object']=='Q30' and warnings[0]['label_provenance']['text']==packets[0]['sentences'][0]['text']
    c['source_document_id']='foreign'
    assert not extraction_warnings(c)
    c.update(source_document_id='document', subject='Q30',object='root length',relation='increased')
    assert not extraction_warnings(c)


def test_nested_code_setting_and_comparative_role_warnings_are_conservative():
    packets=[{'packet_number':1,'sentences':[{'text':'Concentration levels were labelled as Q30.'}]}]
    c={'subject':'root concentration','object':'Q30','relation':'was elevated by increasing the level to',
       'source_evidence_span':'Increasing the level to Q30 elevated root concentration.',
       'source_document_id':'d','source_pdf_sha256':'p'}
    c['source_treatment_labels']=document_treatment_labels(packets,'d','p')
    assert any(w['code']=='possible_treatment_code_endpoint' for w in extraction_warnings(c))
    c={'subject':'Trait Q','object':'Group B','relation':'was consistently higher in',
       'source_evidence_span':'Trait Q in Group A was consistently higher than that in Group B.'}
    assert any(w['code']=='possible_comparison_role_mismatch' for w in extraction_warnings(c))
    c['object']='Group A'
    assert not extraction_warnings(c)


def test_active_trend_effect_is_compared_with_the_outcome_not_the_factor():
    c={'subject':'Factor T','relation':'increased','object':'Trait Q',
       'source_evidence_span':'As Factor T increased, Trait Q and Trait R declined.'}
    assert any(w['code']=='possible_role_inversion' for w in extraction_warnings(c))
    assert c['relation']=='increased'  # Review only, never scientific rewriting.
    c['relation']='decreased'
    assert not extraction_warnings(c)
    c.update(subject='Trait Q',relation='declined as increased',object='Factor T')
    assert not extraction_warnings(c)
    c['relation']='increased as increased'
    assert any(w['code']=='possible_role_inversion' for w in extraction_warnings(c))


@pytest.mark.parametrize('sentence',[
    'Cultivars were designated as AB1 and AB2.',
    'Treatments involved genes designated as StABC1 and StABC2.',
    'Treatments used chemical formulae labelled H2PO4- and HPO4-.',
    'Concentration of Q30 affected growth without an explicit label definition.'])
def test_formula_gene_cultivar_or_unlabelled_names_are_not_guessed(sentence):
    assert not document_treatment_labels([{'packet_number':1,'sentences':[{'text':sentence}]}],'doc','pdf')


def test_deterministic_audit_stratifies_and_never_claims_accuracy():
    rows=[sample_output(i, ('exact','broader','unresolved')[i%3])['row'] for i in range(90)]
    rows[0]['extraction_warnings']=[{'code':'held'}]
    a=application_audit_plan(rows,'doc',catalog());b=application_audit_plan(list(reversed(rows)),'doc',catalog())
    assert len(a['flagged'])==1 and len(a['unflagged'])==30
    assert [o['id'] for o in a['unflagged']]==[o['id'] for o in b['unflagged']]
    assert all(sum(o['row']['bindings']['relation']['mapping']==k for o in a['unflagged'])==10 for k in ('exact','broader','unresolved'))
    labels=[review(o) for o in a['flagged']+a['unflagged']]
    assert audit_review_status(a,labels)['ready']
    for l in labels[1:7]: l.update(atomicity='bundled',disposition='hold')
    assert not audit_review_status(a,labels)['ready']


def test_inverse_property_metadata_stays_upstream_and_missing_definition_stays_absent(tmp_path):
    fixtures=Path('tests/fixtures/ro_influence_pilot')
    ref=prepare_pilot(tmp_path/'catalogue',fetch=lambda url:(fixtures/('pilot_'+url.split('%2F')[-1]+'.rdf')).read_bytes())
    assert len(ref.profile['allowed_classes'])==212 and len(ref.profile['allowed_object_properties'])==101
    t=ref.terms['http://purl.obolibrary.org/obo/RO_0002559']
    assert not t['definition'] and not t['domain'] and not t['range']
    assert t['inverse_of']==['http://purl.obolibrary.org/obo/RO_0002566'] and t['inverse_provenance'] and t['obo_unique_labels']
    claim=sample_output(0)['claim']
    payload,_=request_packet([claim],ref)
    inverse=next(v for v in payload['relations'].values() if v['label']=='causally influenced by')
    assert 'definition' not in inverse and inverse['inverse_of'][0]['definition']
    assert inverse['inverse_of'][0]['domain'] and inverse['inverse_provenance']
    assert len(catalog().profile['allowed_object_properties'])==101


def test_frozen_readiness_rechecks_hashed_automatic_artifacts(tmp_path):
    outputs,labels,controls=boundary_set()
    path=tmp_path/'analysis/experiment/ontology_grounding.jsonl';path.parent.mkdir(parents=True)
    path.write_text('\n'.join(json.dumps(o['row']) for o in outputs),encoding='utf-8')
    r={'frozen_evaluation':{'policy':POLICY,'catalog_signature':catalog().signature,
        'files':[{'path':str(path.relative_to(tmp_path)),'document':'doc','sha256':file_hash(path)}],
        'reviews':labels,'controls':controls}}
    assert verify_frozen_readiness(r,catalog(),tmp_path)['ready']
    path.write_text('',encoding='utf-8')
    assert not verify_frozen_readiness(r,catalog(),tmp_path)['ready']


def test_extraction_rechecks_time_after_source_defined_expansion(tmp_path, monkeypatch):
    from types import SimpleNamespace
    from pypdf import PdfWriter
    from backend.stages.claim_extraction import extractor
    from backend.stages.claim_extraction.targeting import build_extraction_targets
    pdf=tmp_path/'source.pdf'
    writer=PdfWriter();writer.add_blank_page(width=595,height=842);writer.write(pdf)
    packet={'packet_number':1,'section_heading':'Results','page_start':1,'page_end':1,'word_count':16,'estimated_token_count':40,
        'sentences':[{'text':'Measurements use days after transplantation (DAT).','page':1},
                     {'text':'Root length declined from 2 DAT to 4 DAT.','page':1}]}
    target=build_extraction_targets([packet],200)[0]
    proposal={'subject':'Root length','relation':'declined from','object':'2 DAT to 4 DAT',
              'qualifiers':{},'evidence_sentence_ids':[2]}
    monkeypatch.setattr(extractor,'_iter_packet_extractions',lambda *a,**kw:iter([(target,{},[proposal])]))
    claims,_,_=extractor.extract_claims(pdf,SimpleNamespace(model='offline',settings=SimpleNamespace(extraction_concurrency=2)),
        0,lambda *a:None,packets=[target],prepared_targets=True,extraction_mode='target_context',target_words=200)
    assert len(claims)==1 and 'days after transplantation' in claims[0]['object']
    assert any(w['code']=='possible_temporal_endpoint' for w in claims[0]['extraction_warnings'])
    assert claims[0]['object_source_mention']=='2 DAT to 4 DAT'


def test_subset_readiness_gates_incompatible_and_missing_endpoint_relations():
    ref = catalog()
    base = 'http://purl.obolibrary.org/obo/'
    conflict = output_record('doc', {
        'candidate_id': 'pt-conflict',
        'scope': 'in_scope',
        'bindings': {
            'subject': {'iri': base + 'PECO_0007246', 'mapping': 'broader', 'denotation': 'concept', 'reason': 'ok'},
            'object': {'iri': base + 'TO_0001024', 'mapping': 'broader', 'denotation': 'concept', 'reason': 'ok'},
            'relation': {'iri': base + 'RO_0002566', 'mapping': 'broader', 'reason': 'wrong type'},
        },
        'original_claim': {'candidate_id': 'pt-conflict', 'subject': 'inoculation', 'relation': 'increased',
                           'object': 'phosphorus content', 'qualifiers': {}, 'source_target_id': 't',
                           'source_evidence_sentence_numbers': [1]},
    })
    missing_ep = output_record('doc', {
        'candidate_id': 'pt-missing',
        'scope': 'in_scope',
        'bindings': {
            'subject': {'iri': base + 'PO_0009005', 'mapping': 'exact', 'denotation': 'concept', 'reason': 'ok'},
            'object': {'iri': None, 'mapping': 'unresolved', 'denotation': 'concept', 'reason': 'missing'},
            'relation': {'iri': base + 'RO_0002559', 'mapping': 'broader', 'reason': 'missing obj'},
        },
        'original_claim': {'candidate_id': 'pt-missing', 'subject': 'root', 'relation': 'influenced by',
                           'object': 'factor', 'qualifiers': {}, 'source_target_id': 't',
                           'source_evidence_sentence_numbers': [1]},
    })
    labels = [review(conflict), review(missing_ep)]
    summary = evaluate_readiness([conflict, missing_ep], labels, [], ref,
                                 coverage_ok=True, cases_reviewed=True, require_retention=False)
    assert summary['classified_claims'] == 0
    assert summary['classified_endpoints'] == 3


def test_subset_readiness_holds_activity_warning_and_counts_other_endpoint():
    ref = catalog()
    base = 'http://purl.obolibrary.org/obo/'
    warned = output_record('doc', {
        'candidate_id': 'pt-acp',
        'scope': 'in_scope',
        'bindings': {
            'subject': {'iri': base + 'PATO_0000033', 'mapping': 'broader', 'denotation': 'concept', 'reason': 'ok'},
            'object': {'iri': base + 'GO_0003993', 'mapping': 'exact', 'denotation': 'concept', 'reason': 'acp'},
            'relation': {'iri': base + 'RO_0002410', 'mapping': 'broader', 'reason': 'causal'},
        },
        'original_claim': {
            'candidate_id': 'pt-acp',
            'subject': 'phosphorus concentration',
            'relation': 'affected',
            'object': 'acid phosphatase activity',
            'qualifiers': {},
            'source_target_id': 't',
            'source_evidence_sentence_numbers': [1],
            'source_evidence_span': 'Phosphorus concentration affected acid phosphatase activity in roots.',
        },
    })
    summary = evaluate_readiness([warned], [review(warned)], [], ref,
                                 coverage_ok=True, cases_reviewed=True, require_retention=False)
    assert summary['held'] == 1
    assert summary['classified_claims'] == 0
    assert summary['classified_endpoints'] == 1
