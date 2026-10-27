"""Stop signals are job-scoped, durable, and never scientific judgements."""
import asyncio
import importlib
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import Mock
import httpx
import pytest
from fastapi.testclient import TestClient
from backend.config.settings import Settings
from backend.providers.llm import OpenCodeClient
from backend.shared.cancellation import CancellationToken, PipelineStopped, cancellation_scope, check_cancelled, submit_work
from backend.shared.jobs import ACTIVE_STATUSES
from concurrent.futures import ThreadPoolExecutor


@pytest.fixture
def api(tmp_path,monkeypatch):
    module=importlib.import_module('backend.api.app')
    pdf=tmp_path/'paper.pdf';pdf.write_bytes(b'fixture')
    monkeypatch.setattr(module,'_jobs',{})
    monkeypatch.setattr(module,'_cancellations',{})
    monkeypatch.setattr(module,'_pdf_map',lambda:{'paper':pdf})
    monkeypatch.setattr(module,'_output_dir',lambda _:tmp_path/'output')
    monkeypatch.setattr(module,'refresh_saved_views',lambda *a:None)
    return module,TestClient(module.app),pdf


def add_job(module,pdf,status='queued',identifier='one'):
    module._jobs[identifier]={'id':identifier,'pdf_id':'paper','pdf_path':str(pdf),'status':status,
                              'logs':[],'options':{},'request_progress':{'status':'waiting_for_provider'}}


def test_stop_queued_job_is_idempotent_and_never_starts_pipeline(api,monkeypatch):
    module,client,pdf=api;add_job(module,pdf)
    pipeline=Mock();monkeypatch.setattr(module,'run_pipeline',pipeline)
    first=client.post('/api/jobs/one/stop');second=client.post('/api/jobs/one/stop')
    assert first.status_code==second.status_code==202
    assert first.json()['status']=='stopping' and len(second.json()['logs'])==1
    module._worker('one',module.RunOptions())
    assert module._jobs['one']['status']=='stopped'
    assert module._jobs['one']['error'] is None
    assert module._jobs['one']['request_progress']['status']=='cancelled'
    assert 'one' not in module._cancellations
    pipeline.assert_not_called()
    assert client.get('/api/jobs/one').json()['status']=='stopped'


def test_stop_does_not_affect_another_or_finished_job(api):
    module,client,pdf=api;add_job(module,pdf,'running','active');add_job(module,pdf,'completed','old')
    assert client.post('/api/jobs/missing/stop').status_code==404
    assert client.post('/api/jobs/old/stop').json()['status']=='completed'
    assert module._jobs['active']['status']=='running' and not module._cancellations


def test_stopping_blocks_new_runs_until_workers_release(api,monkeypatch):
    module,client,pdf=api;add_job(module,pdf,'stopping')
    monkeypatch.setattr(module,'OpenCodeClient',lambda *_:SimpleNamespace(health_check=lambda:{'models':list(Settings().task_models.values())}))
    assert 'stopping' in ACTIVE_STATUSES
    assert client.post('/api/pdfs/paper/run',json={}).status_code==409


def test_stop_after_a_completed_step_preserves_checkpoint_bytes(api,monkeypatch):
    module,client,pdf=api;add_job(module,pdf)
    folder=module._output_dir(pdf);folder.mkdir()
    saved=folder/'candidates.jsonl';saved.write_bytes(b'{"candidate_id":"saved"}\n')
    def pipeline(*args,**kwargs):
        client.post('/api/jobs/one/stop')
        check_cancelled()
    monkeypatch.setattr(module,'run_pipeline',pipeline)
    module._worker('one',module.RunOptions())
    assert saved.read_bytes()==b'{"candidate_id":"saved"}\n'
    assert module._jobs['one']['status']=='stopped'


def test_token_follows_worker_context_but_does_not_leak_to_other_jobs():
    token=CancellationToken()
    with ThreadPoolExecutor(max_workers=1) as executor:
        with cancellation_scope(token):
            token.request()
            future=submit_work(executor,lambda:'must not execute')
        with pytest.raises(PipelineStopped):future.result()
        assert executor.submit(lambda:check_cancelled() or 'unaffected').result()=='unaffected'


def test_stop_interrupts_local_provider_wait_without_retry_or_scientific_result(monkeypatch):
    started,closed=Event(),Event();calls=[]
    async def receive(request):
        calls.append(request);started.set()
        try:
            await asyncio.sleep(3600)
        finally:
            closed.set()
    original=httpx.AsyncClient
    monkeypatch.setattr('backend.providers.inference.httpx.AsyncClient',lambda **kw:original(transport=httpx.MockTransport(receive),**kw))
    token=CancellationToken();metrics=[];outcome=[]
    model=OpenCodeClient(Settings(),task='external',on_request=metrics.append)
    def run():
        with cancellation_scope(token):
            try:model.chat_json('JSON','evidence')
            except PipelineStopped:outcome.append('stopped')
    thread=Thread(target=run,daemon=True);thread.start()
    assert started.wait(3)
    token.request();thread.join(3)
    assert not thread.is_alive() and closed.is_set()
    assert outcome==['stopped'] and len(calls)==1
    assert metrics[0]['status']=='cancelled' and not metrics[0]['recovery_pending']
    assert model._service_error is None
