"""Cache equivalence and concurrency checks without loading models or using HTTP."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from types import SimpleNamespace
import json

from backend.config.settings import Settings
from backend.shared.artifacts import save_json
from backend.shared.evidence_index import EvidenceIndex
from backend.stages.evidence_ranking.passages import SemanticReranker


def fake_ranker(monkeypatch, settings, index=None, revision='weights-v1'):
    model = SimpleNamespace(config=SimpleNamespace(_commit_hash=revision), calls=[])
    def predict(pairs, **kwargs):
        model.calls.extend(pairs)
        return [float(len(set(q.split()) & set(t.split()))) for q,t in pairs]
    model.predict = predict
    ranker = SemanticReranker(settings)
    ranker.evidence_index = index
    monkeypatch.setattr(ranker,'_load_cross_encoder',lambda:('sentence-transformers',model))
    monkeypatch.setattr(ranker,'rank_texts',lambda q,texts:[float(len(set(q.split()) & set(t.split()))) for t in texts])
    return ranker,model


def test_cross_encoder_scores_reuse_exact_pairs_across_restarts_and_invalidate_changed_weights(tmp_path,monkeypatch):
    settings=Settings()
    index=EvidenceIndex(tmp_path/'cache')
    first,model=fake_ranker(monkeypatch,settings,index)
    expected=first.cross_scores('crop outcome',['crop outcome','other passage','crop outcome'])
    assert len(model.calls)==2
    assert first.cross_scores('crop outcome',['crop outcome','other passage'])==expected[:2]
    assert len(model.calls)==2
    restarted,new_model=fake_ranker(monkeypatch,settings,index)
    assert restarted.cross_scores('crop outcome',['crop outcome','other passage'])==expected[:2]
    assert new_model.calls==[]
    changed,new_model=fake_ranker(monkeypatch,settings,index,revision='weights-v2')
    changed.cross_scores('crop outcome',['crop outcome'])
    assert len(new_model.calls)==1
    restarted.cross_scores('different query',['crop outcome'])
    assert len(new_model.calls)==1


def test_batched_passage_ranking_preserves_serial_results_and_all_source_text(monkeypatch):
    settings=Settings(evidence_index_enabled=False)
    serial,_=fake_ranker(monkeypatch,settings)
    batched,_=fake_ranker(monkeypatch,settings)
    chunks=[{'text':'Crop C affects process B.','page_number':1},
            {'text':'Process B changes outcome E.','section_heading':'Results'},
            {'text':'Other conditions do not establish this relationship.'}]
    queries=[('Crop C process B','crop process'),('process B outcome E','process outcome')]
    expected=[serial.select(q,chunks,'paper',2,neutral_query=n) for q,n in queries]
    assert batched.select_many(queries,chunks,'paper',2)==expected
    assert all(row['text'] in [c['text'] for c in chunks] for result in expected for row in result)


def test_parallel_ranking_does_not_duplicate_scores_or_mix_queries(monkeypatch):
    ranker,model=fake_ranker(monkeypatch,Settings(evidence_index_enabled=False))
    with ThreadPoolExecutor(max_workers=4) as workers:
        outputs=list(workers.map(lambda _:ranker.cross_scores('crop',['crop','different']),range(12)))
    assert all(row==[1.0,0.0] for row in outputs)
    assert len(model.calls)==2


def test_shared_artifact_temp_files_are_serialized_across_workers(tmp_path):
    path=tmp_path/'new-directory'/'shared.json'
    with ThreadPoolExecutor(max_workers=4) as workers:
        list(workers.map(lambda i:save_json(path,{'writer':i,'content':'x'*1000}),range(50)))
    result=json.loads(path.read_text(encoding='utf-8'))
    assert result['writer'] in range(50) and result['content']=='x'*1000
    assert not path.with_name('shared.json.tmp').exists()


def test_source_batches_overlap_without_sharing_candidate_proof():
    from threading import Barrier
    from backend.shared.batching import iter_parallel_judgments
    barrier=Barrier(2)
    def validate(candidate,client):
        barrier.wait(timeout=5)
        return {'candidate_id':candidate['candidate_id'],'proof':candidate['subject']}
    candidates=[{'candidate_id':'one','subject':'Entity A'}, {'candidate_id':'two','subject':'Entity B'}]
    rows=list(iter_parallel_judgments(candidates,SimpleNamespace(settings=Settings()),validate,batch_size=1,workers=2))
    assert {c['candidate_id']:r['proof'] for c,r in rows}=={'one':'Entity A','two':'Entity B'}


def test_worker_corrections_reach_only_the_published_source_claim():
    from threading import Barrier
    from backend.shared.batching import iter_parallel_judgments
    barrier=Barrier(2)
    def validate(candidate,client):
        candidate['subject']='Expanded '+candidate['subject']
        barrier.wait(timeout=5)
        return {'candidate_id':candidate['candidate_id'],'decision':'entailed'}
    candidates=[{'candidate_id':'one','subject':'A'},{'candidate_id':'two','subject':'B'}]
    results=iter_parallel_judgments(candidates,SimpleNamespace(settings=Settings()),validate,batch_size=1,workers=2)
    published,_=next(results)
    assert published is next(c for c in candidates if c['candidate_id']==published['candidate_id'])
    assert sum(c['subject'].startswith('Expanded ') for c in candidates)==1
    list(results)
    assert all(c['subject'].startswith('Expanded ') for c in candidates)
