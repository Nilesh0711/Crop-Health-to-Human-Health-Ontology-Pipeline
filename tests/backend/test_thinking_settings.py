"""Stage thinking controls reach HTTP, recovery, cache identity and resume."""
from dataclasses import replace
import importlib
import json
from types import SimpleNamespace
from unittest.mock import Mock
import pytest
from pydantic import ValidationError
from backend.config.settings import Settings, TASK_DEFAULT_MODELS
from backend.providers.inference import InferenceHTTP
from backend.providers.llm import OpenCodeClient, request_log_level
from backend.api.app import RunOptions
from backend.pipelines.checkpoints import claim_stage_signature
from backend.stages.external_validation.workflow import prepare_evidence, validation_signature


@pytest.mark.parametrize('stage', list(TASK_DEFAULT_MODELS))
@pytest.mark.parametrize('enabled', [True, False])
def test_each_stage_sends_explicit_thinking_and_records_requested_vs_observed(monkeypatch,stage,enabled):
    configuration = replace(Settings(), thinking={stage:enabled})
    payloads, metrics, progress = [], [], []
    def receive(self,method,url,**kwargs):
        payloads.append(kwargs['json'])
        return {'choices':[{'finish_reason':'stop','message':{'content':'{"ok":true}'}}],
                'usage':{'prompt_tokens':10,'completion_tokens':5,'completion_tokens_details':{'reasoning_tokens':0}}}
    monkeypatch.setattr(InferenceHTTP,'_request',receive)
    client = OpenCodeClient(configuration, task=stage, on_request=metrics.append,on_progress=progress.append)
    assert client.chat_json('Return JSON','input') == {'ok':True}
    assert payloads[0]['thinking'] == {'type':'enabled' if enabled else 'disabled'}
    assert metrics[0]['requested_thinking'] == ('enabled' if enabled else 'disabled')
    assert metrics[0]['thinking_observed'] is False
    assert progress[0]['requested_thinking'] == metrics[0]['requested_thinking']
    assert all(mode is False for key,mode in configuration.task_thinking.items() if key != stage)


def test_all_defaults_off_and_strict_api_values():
    assert not any(RunOptions().thinking.model_dump().values())
    assert not any(Settings().task_thinking.values())
    assert RunOptions(thinking={'external':True}).thinking.extraction is False
    assert RunOptions(thinking={'external':True}).thinking.external is True
    for invalid in ({'external':'false'}, {'external':0}, {'invented':False}):
        with pytest.raises(ValidationError): RunOptions(thinking=invalid)


def test_recovery_requests_keep_the_same_stage_thinking_mode(monkeypatch):
    bodies=[]
    def receive(self,method,url,**kwargs):
        bodies.append(kwargs['json'])
        return {'choices':[{'finish_reason':'length' if len(bodies)==1 else 'stop',
                            'message':{'content':'' if len(bodies)==1 else '{"ok":true}'}}], 'usage':{}}
    monkeypatch.setattr(InferenceHTTP,'_request',receive)
    client=OpenCodeClient(replace(Settings(),thinking={'external':False}),task='external')
    assert client.chat_json('JSON','original evidence') == {'ok':True}
    assert len(bodies)==2 and all(b['thinking']=={'type':'disabled'} for b in bodies)


def test_off_invalidates_only_relevant_judge_cache_and_reuses_retained_papers(monkeypatch):
    configuration=replace(Settings(),thinking={stage:True for stage in TASK_DEFAULT_MODELS}); off=replace(configuration,thinking={**configuration.task_thinking,'external':False})
    candidate={'candidate_id':'c','subject':'wheat','relation':'has_contaminant','object':'toxin'}
    original=validation_signature(candidate,configuration)
    assert original != validation_signature(candidate,off)
    assert original != validation_signature(candidate,replace(configuration,thinking={**configuration.task_thinking,'query':False}))
    assert original == validation_signature(candidate,replace(configuration,thinking={**configuration.task_thinking,'pathway_assembly':False}))
    old={'validation_signature':original,'attempts':[{'evidence_documents':[{'evidence_document_id':'p1'}],'decisions':[{'old':True}]}]}
    retrieve=Mock(side_effect=AssertionError('Compatible papers must not be downloaded again'))
    monkeypatch.setattr('backend.stages.external_validation.workflow.retrieve_round',retrieve)
    prepared=prepare_evidence(candidate,None,None,off,resume_record=old)
    assert prepared['evidence_documents']==old['attempts'][0]['evidence_documents']
    assert 'decisions' not in prepared
    # Retrieval keeps its architecture-independent historical fingerprint;
    # the new per-paper judgement signature also includes its proof policy.
    assert prepared['retrieval_signature']==validation_signature(candidate,replace(configuration,external_judgement_mode='joint'))
    assert prepared['prepared_validation_signature']==validation_signature(candidate,off)
    assert old['attempts'][0]['decisions']==[{'old':True}]
    assert claim_stage_signature(candidate,'model') != claim_stage_signature(candidate,'model',False)


def test_ignored_off_setting_is_a_visible_warning():
    assert request_log_level({'status':'ok','requested_thinking':'disabled','reasoning_tokens':10})=='warning'


def test_runtime_worker_forwards_thinking_to_each_action(tmp_path,monkeypatch):
    module=importlib.import_module('backend.api.app')
    options=RunOptions(thinking={'external':False,'pathway_assembly':False})
    monkeypatch.setattr(module,'_output_dir',lambda _:tmp_path)
    monkeypatch.setattr(module,'_persist_job',lambda *_:None)
    monkeypatch.setattr(module,'_emit',lambda *_:None)
    for action in (None,'retry_external','assemble_pathways'):
        monkeypatch.setattr(module,'_jobs',{'j':{'pdf_path':str(tmp_path/'a.pdf'),'status':'queued','action':action,'candidate_id':'c'}})
        full,claim,assembly=(Mock(return_value={'run_status':'completed'}) for _ in range(3))
        monkeypatch.setattr(module,'run_pipeline',full);monkeypatch.setattr(module,'run_claim_action',claim);monkeypatch.setattr(module,'assemble_saved_pathways',assembly)
        module._worker('j',options)
        invoked=full if action is None else assembly if action=='assemble_pathways' else claim
        assert invoked.call_args.kwargs['thinking']==options.thinking.model_dump()
