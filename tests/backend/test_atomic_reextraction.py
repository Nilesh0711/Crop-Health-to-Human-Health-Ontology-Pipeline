"""Deliberate multi-child repair, durable provenance and checkpoint recovery."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace

import pytest

from backend.config.settings import Settings, SCHEMA_VERSION
from backend.pipelines.claim_repair import run_claim_action
from backend.pipelines.checkpoints import resume_metadata
from backend.shared.artifacts import save_json, save_rows
from backend.shared.storage import file_hash, read_jsonl
from backend.stages.source_validation.scope import source_pathway_eligible
from backend.providers.llm import LLMServiceError


@pytest.fixture
def atomic_case(tmp_path,monkeypatch):
    pdf=tmp_path/'source.pdf'; pdf.write_bytes(b'Original PDF fixture; parsing is mocked.')
    output=tmp_path/'ontology'/'source'; output.mkdir(parents=True)
    c={'candidate_id':'parent','subject':'Factor A','relation':'changes','object':'Process P',
       'subject_type':'PlantTrait','object_type':'PlantProcess','certainty':'descriptive','qualifiers':{},
       'claim_text':'Factor A changes Process P and Outcome Q.','schema_error':None,
       'source_pdf_sha256':file_hash(pdf),'source_page':1,'source_page_end':1,'source_packet_number':1,
       'source_evidence_sentence_numbers':[1],'source_evidence_span':'Factor A changes Process P and Outcome Q.',
       'source_validation':{'decision':'needs_review','rationale':'Two independent outcomes.'}}
    unrelated={**deepcopy(c),'candidate_id':'unrelated'}
    save_rows(output/'candidates.jsonl',[c,unrelated])
    packet={'packet_number':1,'section_heading':'Results','sentences':[{'text':c['source_evidence_span'],'page':1}]}
    save_rows(output/'extraction_contexts.jsonl',[packet])
    settings=replace(Settings(),source_workers=1)
    metadata={'schema_version':SCHEMA_VERSION,'pdf_sha256':file_hash(pdf),'task_models':settings.task_models,
        'max_pages':0,'claims_per_packet':0,'extraction_complete':True,'extraction_mode':'whole_packet',
        'thinking':settings.task_thinking,'candidate_ids':['parent','unrelated'],'knowledge_building_mode':'pathway_only'}
    save_json(output/'pipeline_checkpoint.json',metadata)
    children=[]
    for n,obj in enumerate(['Process P','Outcome Q']):
        child=deepcopy(c);child.pop('source_validation');child.update(candidate_id=f'proposal-{n}',object=obj,
            claim_text=f'Factor A changes {obj}.')
        children.append(child)
    module='backend.pipelines.claim_repair.'
    monkeypatch.setattr(module+'OpenCodeClient',lambda *args,**kwargs:SimpleNamespace(settings=settings))
    monkeypatch.setattr(module+'extract_claims',lambda *args,**kwargs:(deepcopy(children),{},[]))
    monkeypatch.setattr(module+'validate_source_claim',lambda candidate,client:{'candidate_id':candidate['candidate_id'],
        'decision':'entailed','project_scope':{'decision':'in_scope','rationale':'Source-grounded crop mechanism.'}})
    monkeypatch.setattr(module+'assemble_pathways',lambda *args,**kwargs:{'graph':{'nodes':[],'edges':[]}})
    monkeypatch.setattr(module+'refresh_saved_views',lambda *args:{})
    monkeypatch.setattr('backend.pipelines.neighbor_validation.run_pathway_assessment',lambda *args,**kwargs:{'outcomes':{}})
    return SimpleNamespace(pdf=pdf,output=output,parent=c,unrelated=unrelated,children=children,settings=settings,metadata=metadata)


def test_atomic_children_preserve_original_fields_and_exclude_only_the_superseded_parent(atomic_case):
    c=atomic_case
    result=run_claim_action(c.pdf,c.output,'parent','reextract',8,lambda *args:None)
    rows=read_jsonl(c.output/'candidates.jsonl')
    assert len(rows)==4 and rows[1]==c.unrelated
    assert {k:v for k,v in rows[0].items() if k!='superseded_by'}==c.parent
    assert rows[0]['superseded_by']==result['child_candidate_ids']
    assert not source_pathway_eligible(rows[0])
    assert all(source_pathway_eligible(child) for child in rows[2:])
    assert all(child['parent_candidate_id']=='parent' for child in rows[2:])
    assert read_jsonl(c.output/'claim_revisions.jsonl')[0]['original_candidate']==c.parent
    metadata=resume_metadata(c.output,c.pdf,c.settings,0,0,extraction_mode='whole_packet')
    assert metadata['candidate_ids']==[r['candidate_id'] for r in rows]


def test_interrupted_checkpoint_update_recovers_only_journal_bound_children(atomic_case):
    c=atomic_case
    run_claim_action(c.pdf,c.output,'parent','reextract',8,lambda *args:None)
    save_json(c.output/'pipeline_checkpoint.json',c.metadata)  # Simulate interruption after candidate publication.
    recovered=resume_metadata(c.output,c.pdf,c.settings,0,0,extraction_mode='whole_packet')
    assert len(recovered['candidate_ids'])==4
    rows=read_jsonl(c.output/'candidates.jsonl')
    rows[-1]['qualifiers']={'condition':'Invented condition'}
    save_rows(c.output/'candidates.jsonl',rows)
    with pytest.raises(ValueError,match='do not match'):
        resume_metadata(c.output,c.pdf,c.settings,0,0,extraction_mode='whole_packet')


def test_source_failure_before_publication_preserves_every_original_claim(atomic_case,monkeypatch):
    c=atomic_case; before=(c.output/'candidates.jsonl').read_bytes()
    def fail(*args): raise LLMServiceError('Source provider unavailable')
    monkeypatch.setattr('backend.pipelines.claim_repair.validate_source_claim',fail)
    with pytest.raises(LLMServiceError): run_claim_action(c.pdf,c.output,'parent','reextract',8,lambda *args:None)
    assert (c.output/'candidates.jsonl').read_bytes()==before
    assert read_jsonl(c.output/'claim_revisions.jsonl')[0]['status']=='failed'


def test_later_assessment_failure_preserves_published_children_and_resumable_checkpoint(atomic_case,monkeypatch):
    c=atomic_case
    def fail(*args,**kwargs): raise LLMServiceError('External provider unavailable')
    monkeypatch.setattr('backend.pipelines.neighbor_validation.run_pathway_assessment',fail)
    with pytest.raises(LLMServiceError):
        run_claim_action(c.pdf,c.output,'parent','reextract',8,lambda *args:None)
    rows=read_jsonl(c.output/'candidates.jsonl')
    assert len(rows)==4 and rows[0]['superseded_by']
    assert all(child.get('source_validation') for child in rows[2:])
    metadata=resume_metadata(c.output,c.pdf,c.settings,0,0,extraction_mode='whole_packet')
    assert metadata['candidate_ids']==[c['candidate_id'] for c in rows]
    assert read_jsonl(c.output/'claim_revisions.jsonl')[0]['status']=='failed'


def test_unrelated_packet_sentence_is_never_published_as_a_child(atomic_case,monkeypatch):
    c=atomic_case; before=(c.output/'candidates.jsonl').read_bytes()
    c.children[0]['source_evidence_sentence_numbers']=[2]
    with pytest.raises(ValueError,match='parent sentence'):
        run_claim_action(c.pdf,c.output,'parent','reextract',8,lambda *args:None)
    assert (c.output/'candidates.jsonl').read_bytes()==before
