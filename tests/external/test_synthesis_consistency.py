"""Complete but contradictory verdicts remain reviewable without invented support."""
from copy import deepcopy
from dataclasses import replace
import json
from threading import Barrier, Event

import pytest

from backend.providers.llm import OpenCodeClient
from backend.providers.structured_output import EvidenceSelectionError
from backend.shared.cancellation import check_cancelled
from backend.stages.neighbor_validation.judgement import check_synthesis, ground_paper, synthesize
from backend.stages.neighbor_validation.records import DIRECTORY, read_records
from tests.external.test_neighbor_validation import case, Client, extend, fake_retrieval, paper_answer, synthesis_answer


def partial_answer(case):
    answer=synthesis_answer(case)
    for edge in answer['edge_assessments']:
        edge.update(stance='partially_supported',reason='The original qualifier is not established.')
    return answer


def install_responses(case,monkeypatch,answers):
    calls,metrics=[],[]
    pending=iter(answers)
    client=OpenCodeClient(case.settings,task='external',on_request=metrics.append)
    def reply(*args):
        calls.append(args)
        answer=deepcopy(next(pending))
        answer.pop('pathway_id',None)  # Attached from the fixed request by the stage.
        return {'content':json.dumps(answer),'finish_reason':'stop',
                'usage':{'prompt_tokens':200,'completion_tokens':100,
                         'completion_tokens_details':{'reasoning_tokens':20}}}
    monkeypatch.setattr(client._inference,'chat',reply)
    return client,calls,metrics


@pytest.mark.parametrize('conflict',['partial_pair','scope','empty_components','missing_incompatibility'])
def test_complete_inconsistent_verdicts_are_reviewable_and_never_rewritten(case,conflict):
    paper=ground_paper(case.target,case.document,paper_answer(case))
    answer=partial_answer(case) if conflict=='partial_pair' else synthesis_answer(case)
    if conflict=='scope':
        answer.update(verdict='out_of_scope',in_scope=True)
    elif conflict=='empty_components':
        answer.update(verdict='components_supported',evidence_basis='component_evidence_only')
        for edge in answer['edge_assessments']: edge.update(stance='unresolved',proof_refs=[])
    elif conflict=='missing_incompatibility':
        answer['verdict']='incompatible'
    original=deepcopy(answer)
    with pytest.raises(EvidenceSelectionError):
        check_synthesis(case.target,[paper],answer)
    assert answer==original


def test_failed_verdict_regeneration_keeps_proposal_and_does_not_poison_client(case,monkeypatch):
    paper=ground_paper(case.target,case.document,paper_answer(case))
    bad=partial_answer(case)
    client,calls,metrics=install_responses(case,monkeypatch,[bad,bad,synthesis_answer(case)])
    with pytest.raises(EvidenceSelectionError,match='partially_supported is insufficient') as error:
        synthesize(client,case.target,[paper],case.settings)
    assert error.value.review_response==bad
    assert [m['status'] for m in metrics]==['evidence_selection_error','evidence_needs_review']
    assert len(calls)==2 and calls[0][2]==calls[1][2]
    assert all(call[0]==case.settings.external_model for call in calls)
    assert 'partially_supported is insufficient' in calls[1][1]
    assert synthesize(client,case.target,[paper],case.settings)['verdict']=='connection_supported'
    assert len(calls)==3 and metrics[-1]['status']=='ok'


def test_model_can_correct_only_overall_verdict_without_upgrading_partial_edges(case,monkeypatch):
    paper=ground_paper(case.target,case.document,paper_answer(case))
    bad=partial_answer(case)
    corrected={**deepcopy(bad),'verdict':'components_supported','evidence_basis':'component_evidence_only'}
    client,calls,metrics=install_responses(case,monkeypatch,[bad,corrected])
    result=synthesize(client,case.target,[paper],case.settings)
    assert result==corrected
    assert len(calls)==2 and metrics[-1]['status']=='ok'
    assert all(edge['stance']=='partially_supported' for edge in result['edge_assessments'])


def test_held_inconsistent_pair_does_not_cancel_a_concurrent_pair(case,monkeypatch):
    import backend.pipelines.neighbor_validation as pipeline
    import backend.stages.neighbor_validation.workflow as workflow
    edge=extend(case,'n2','n4',31)
    graph={**case.graph,'edges':[*case.graph['edges'],edge],
           'nodes':[*case.graph['nodes'],{'id':'n4','label':'Outcome F','type':'PlantTrait'}]}
    settings=replace(case.settings,neighbor_workers=2)
    client,calls,_=install_responses(case,monkeypatch,[partial_answer(case),partial_answer(case)])
    monkeypatch.setattr(workflow,'retrieve_pathway',fake_retrieval(case))
    monkeypatch.setattr(workflow,'judge_paper',lambda *args:ground_paper(case.target,case.document,paper_answer(case)))
    class Ranker:
        embedding_cache_hits=0
        def __init__(self,*args): pass
        def warmup(self): pass
    monkeypatch.setattr(pipeline,'SemanticReranker',Ranker)
    barrier,held=Barrier(2),Event()
    def assess(target,*args,**kwargs):
        barrier.wait(timeout=5)
        if target['pathway_id']==case.target['pathway_id']:
            try:
                return workflow.assess_pathway(target,*args,**kwargs)
            finally:
                held.set()
        assert held.wait(5)
        check_cancelled()
        row={'pathway_id':target['pathway_id'],'target':target,'verdict':'unresolved'}
        args[6](row)
        return row
    monkeypatch.setattr(pipeline,'assess_pathway',assess)
    result=pipeline.run_pathway_assessment(case.root,case.root/'Document 1',{'graph':graph},
        {'query':Client(case),'external':client},settings,lambda *args:None)
    assert result['assessed_pairs']==2 and len(calls)==2
    assert result['outcomes']=={'assessment_incomplete':1,'unresolved':1}
    record=next(r for r in read_records(case.root,graph=graph) if r['pathway_id']==case.target['pathway_id'])
    assert record['paper_assessments'] and not record['eligible']
    assert record['review_response']['verdict']=='connection_supported'
    assert all(edge['stance']=='partially_supported' for edge in record['review_response']['edge_assessments'])
    assert (case.root/DIRECTORY/'results.jsonl').exists()
