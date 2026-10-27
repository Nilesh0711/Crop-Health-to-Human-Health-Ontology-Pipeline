"""A research override must not alter unrelated request fields or production defaults."""
from copy import deepcopy

import pytest

from backend.config.settings import Settings
from backend.providers.inference import InferenceHTTP
from scripts.research.reasoning_comparison import ResearchThinkingHTTP


@pytest.mark.parametrize('mode',['provider_default','disabled'])
def test_pilot_changes_only_the_requested_thinking_parameter(monkeypatch,mode):
    captured=[]
    def receive(self,method,url,**kwargs):
        captured.append((method,url,kwargs))
        return {'received':True}
    monkeypatch.setattr(InferenceHTTP,'_request',receive)
    body=dict(model='deepseek-v4.1-flash',messages=[dict(role='user',content='Original source evidence')],
              max_tokens=12288,temperature=0,response_format=dict(type='json_object'))
    original=deepcopy(body)
    adapter=ResearchThinkingHTTP(Settings(),mode)
    assert adapter._request('POST','https://example.test',json=body)=={'received':True}
    assert body==original
    expected=deepcopy(original)
    if mode=='disabled':
        expected['thinking']={'type':'disabled'}
    assert captured[0][2]['json']==expected


def test_disabled_pilot_never_substitutes_a_different_model(monkeypatch):
    def forbidden(*args,**kwargs):
        raise AssertionError('Wrong model must stop before provider access')
    monkeypatch.setattr(InferenceHTTP,'_request',forbidden)
    adapter=ResearchThinkingHTTP(Settings(),'disabled')
    with pytest.raises(ValueError,match='restricted to DeepSeek'):
        adapter._request('POST','https://example.test',json={'model':'mimo-v2.6-flash'})


def test_research_override_does_not_change_catalog_requests(monkeypatch):
    captured=[]
    monkeypatch.setattr(InferenceHTTP,'_request',lambda self,method,url,**kwargs:captured.append(kwargs))
    ResearchThinkingHTTP(Settings(),'disabled')._request('GET','https://example.test/models')
    assert captured==[{}]


def test_production_http_keeps_provider_default(monkeypatch):
    captured=[]
    def receive(self,method,url,**kwargs):
        captured.append(kwargs['json'])
        return dict(choices=[dict(finish_reason='stop',message=dict(content='{}'))],usage={})
    monkeypatch.setattr(InferenceHTTP,'_request',receive)
    InferenceHTTP(Settings()).chat('opencode-go/deepseek-v4.1-flash','System','Source',{},12288)
    assert 'thinking' not in captured[0]


@pytest.mark.parametrize('numbers,valid',[([61],True),([61,62],True),([61,80],False),([],False)])
def test_target_preserves_context_and_rejects_outside_evidence(monkeypatch,numbers,valid):
    from backend.providers.llm import OpenCodeClient
    from backend.providers.structured_output import IncompleteResponseError
    from scripts.research.reasoning_comparison import TargetedExtractionClient
    captured=[]
    def receive(self,system,prompt,**kwargs):
        captured.append((system,prompt))
        value={'claims':[{'evidence_sentence_numbers':numbers}]}
        kwargs['response_check'](value)
        return value
    monkeypatch.setattr(OpenCodeClient,'chat_json',receive)
    client=TargetedExtractionClient(Settings(),task='extraction',target_sentence_numbers=[61,62])
    if valid:
        assert client.chat_json('Original schema and science rules','Full original packet')['claims']
    else:
        with pytest.raises(IncompleteResponseError,match='outside the extraction target'):
            client.chat_json('Original schema and science rules','Full original packet')
    assert captured[0][1]=='Full original packet'
    assert captured[0][0].startswith('Original schema and science rules')
    assert 'SPECIFIC EXTRACTION TARGET' in captured[0][0]
