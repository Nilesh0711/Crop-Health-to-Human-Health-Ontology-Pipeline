"""Neighbor-pair contracts, branch locality, proof safety, evidence reuse and resume."""
from copy import deepcopy
from dataclasses import replace
import json
from types import SimpleNamespace
from pathlib import Path

import pytest

from backend.config.settings import Settings
from backend.shared.artifacts import save_rows, save_json
from backend.shared.cache import RunCache
from backend.providers.structured_output import EvidenceSelectionError, IncompleteResponseError
from backend.providers.llm import LLMServiceError
from backend.providers.response_repair import validate_schema
from backend.stages.neighbor_validation.targets import enumerate_targets, load_candidates, make_target
from backend.stages.neighbor_validation.judgement import ground_paper, check_synthesis, paper_schema, synthesis_schema
from backend.stages.neighbor_validation.records import read_records, validated_graph, validated_paths, save_record, DIRECTORY
from backend.stages.neighbor_validation.workflow import assess_pathway
from backend.stages.query_planning.neighbors import translate_pathway, followup_queries
from backend.stages.neighbor_validation.retrieval import retrieval_candidate


@pytest.fixture
def case(tmp_path):
    root = tmp_path / 'ontology'
    edges = []
    for n,(subject,obj) in enumerate([('Crop C','Intermediate B'),('Intermediate B','Outcome E')],1):
        document = f'Document {n}'
        candidate = {'candidate_id':f'claim-{n}','subject':subject,'object':obj,'relation':'increases',
            'subject_type':'PlantTrait','object_type':'PlantTrait','certainty':'conditional','qualifiers':{'condition':'Condition K'},
            'schema_error':None,'source_validation':{'decision':'entailed', 'project_scope': {'decision':'in_scope', 'rationale':'A source-grounded crop mechanism.'}},'source_page':n,'source_pdf_sha256':f'pdf-{n}',
            'source_evidence_span':f'{subject} increases {obj} under Condition K.',
            'source_local_context':f'Context for {subject} and {obj}.',
            'source_paper_title':document,'source_references':[{'doi':f'10.example/ref{n}','citation_text':f'Reference {n}'}]}
        save_rows(root / document / 'candidates.jsonl',[candidate])
        edges.append({'id':f'edge-{n}','candidate_id':candidate['candidate_id'],'document_key':document,
            'source':f'n{n}','target':f'n{n+1}','source_label':subject,'target_label':obj,**{k:candidate[k] for k in ('subject','object','relation','qualifiers','certainty')}})
    graph = {'nodes':[{'id':f'n{n}','label':label,'type':'PlantTrait','endpoint_ids':[]} for n,label in enumerate(['Crop C','Intermediate B','Outcome E'],1)],'edges':edges}
    target = make_target(edges,load_candidates(root))
    document = {'evidence_document_id':'paper-p','title':'Independent experimental study','doi':'10.example/p',
        'evidence_level':'independent_external','evidence_access':'full_text','independence_group':'study-p',
        'evidence_chunks':[{'chunk_id':'chunk-p','text':'Crop C increases Intermediate B under Condition K. Intermediate B increases Outcome E under Condition K. Blocking Intermediate B prevents the effect of Crop C on Outcome E.'}]}
    settings = Settings(evidence_index_enabled=False,neighbor_workers=1,pathway_followup_papers=0)
    return SimpleNamespace(root=root,target=target,graph=graph,document=document,settings=settings,tmp=tmp_path)


def paper_answer(case, **changes):
    answer = {'pathway_id':case.target['pathway_id'],'evidence_document_id':case.document['evidence_document_id'],
        'finding':'Applicable local findings.',
        'edge_findings':[{'edge_id':e['id'],'stance':'supports','claim_match':'exact','conditions_match':True,'proof_ids':['proof-p'],'reason':'Experimental finding.'} for e in case.target['edges']],
        'connection_findings':[{'connection_id':r['connection_id'],'compatibility':'compatible','proof_ids':['proof-p'],'reason':'The measured intermediate is shared.'} for r in case.target['connections']],
        'proofs':[{'proof_id':'proof-p','chunk_id':'chunk-p','sentence_numbers':[1,2,3]}]}
    return {**answer,**changes}


def synthesis_answer(case, **changes):
    refs = [{'evidence_document_id':case.document['evidence_document_id'],'proof_id':'proof-p'}]
    answer = {'pathway_id':case.target['pathway_id'],'verdict':'connection_supported','evidence_basis':'local_connection_support',
        'in_scope':True,'scope_reason':'Crop pathway evidence.','synthesis':'The tested intermediate connects this route under Condition K.','corrections':[],
        'limitations':['Applies to the reported conditions.'],
        'edge_assessments':[{'edge_id':e['id'],'stance':'supported','proof_refs':refs,'reason':'Bounded experimental evidence.'} for e in case.target['edges']],
        'connection_assessments':[{'connection_id':c['connection_id'],'connection_kind':'mechanistic','compatibility':'compatible','proof_refs':refs,'reason':'Intermediate experimentally examined.'} for c in case.target['connections']],
        'entity_roles':[{'node_id':node,'contextual_role':'Contextual entity','proposed_type':'PlantTrait','resolved':True,'proof_refs':refs,'reason':'Measured in the supplied study.'} for node in ['n1','n2','n3']]}
    return {**answer,**changes}


class Client:
    def __init__(self,case,model=None,calls=None):
        self.case,self.model,self.calls=case,model or case.settings.external_model,calls if calls is not None else []
    def for_model(self,model):
        return Client(self.case,model,self.calls)
    def chat_json(self,system,user,**kwargs):
        payload=json.loads(user);self.calls.append((self.model,kwargs['request_label'],payload))
        if 'neighbor query:' in kwargs['request_label']:
            return {'queries':[{'target_id':r['target_id'],'query':'Crop C Intermediate B Outcome E','openalex_query':'crop intermediate outcome'} for r in payload['retrieval_targets']]}
        if 'neighbor proof audit:' in kwargs['request_label']:
            return {'edge_checks':[{'edge_id':e['edge_id'],'decision':'exact',
                'proof_refs':[{k:p[k] for k in ('evidence_document_id','proof_id')} for p in e['external_proof']],
                'reason':'Synthetic admitted proof covers this fixed proposition.'} for e in payload['edges']],
                'connection_checks':[{'connection_id':c['connection_id'],'decision':'compatible',
                'proof_refs':[{k:p[k] for k in ('evidence_document_id','proof_id')} for p in c['external_proof']],
                'reason':'Synthetic local context is compatible.'} for c in payload['connections']]}
        if 'neighbor paper external:' in kwargs['request_label']:
            answer=paper_answer(self.case)
            pending=payload.get('edge_ids_to_assess')
            if pending is not None: answer['edge_findings']=[r for r in answer['edge_findings'] if r['edge_id'] in pending]
            if payload.get('retained_proofs'):
                old=payload['retained_proofs'][0]['proof_id']
                for row in answer['edge_findings']+answer['connection_findings']: row['proof_ids']=[old]
                answer['proofs']=[]
            return answer
        if 'follow-up query:' in kwargs['request_label']:
            return {'queries':[{'target_id':self.case.target['connections'][0]['connection_id'],'query':'route mechanism Condition K','openalex_query':'crop intermediate mechanism'}]}
        return synthesis_answer(self.case)


def prepared(case,pending=False):
    return {'evidence_documents':[case.document],'evidence_documents_count':1,'retrieval_pending':pending,
            'pathway_coverage':{},'query_translation':{},'citation_ledger':[]}


def fake_retrieval(case,pending=False):
    def retrieve(*args,**kwargs):
        args[-1](case.document)
        return prepared(case,pending)
    return retrieve


def extend(case, source, target, number, *, document='Document 2'):
    candidates = load_candidates(case.root)
    c = deepcopy(next(iter(candidates.values())))
    c.update(candidate_id=f'added-{number}',subject=source,object=target,
             source_evidence_span=f'{source} increases {target} under Condition K.')
    save_rows(case.root/document/'candidates.jsonl',
              [*read_jsonl(case.root/document/'candidates.jsonl'),c])
    return {'id':f'added-edge-{number}','candidate_id':c['candidate_id'],'document_key':document,
        'source':source,'target':target,'source_label':source,'target_label':target,
        **{k:c[k] for k in ('subject','object','relation','qualifiers','certainty')}}


from backend.shared.storage import read_jsonl
from backend.stages.neighbor_validation.targets import AdjacencyIndex, POLICY
from backend.stages.neighbor_validation.findings import FindingCache
from backend.stages.neighbor_validation.judgement import judge_paper


INTERPRETATION_CASES=json.loads((Path(__file__).parents[1]/'fixtures/neighbor_interpretation_cases.json').read_text(encoding='utf-8'))


@pytest.mark.parametrize('example',INTERPRETATION_CASES,ids=lambda row:row['id'])
def test_declared_proposition_matches_control_admissible_verdicts(case,example):
    """Enforce evidence contracts; synthetic declarations are not model accuracy gold."""
    answer=paper_answer(case)
    answer['edge_findings'][0].update({k:example[k] for k in ('stance','claim_match','conditions_match')})
    paper=ground_paper(case.target,case.document,answer)
    proposed=synthesis_answer(case)
    proposed['edge_assessments'][0]['stance']=example['synthesis_stance']
    if example['synthesis_stance']!='supported':
        proposed.update(verdict='components_supported',evidence_basis='component_evidence_only')
    check_synthesis(case.target,[paper],proposed)
    if example['synthesis_stance']!='supported':
        promoted=deepcopy(proposed)
        promoted['edge_assessments'][0]['stance']='supported'
        with pytest.raises(EvidenceSelectionError):check_synthesis(case.target,[paper],promoted)


def test_externally_grounded_correction_never_edits_or_validates_original_claim(case):
    source=case.root/'Document 1'/'candidates.jsonl'
    before=source.read_bytes()
    answer=paper_answer(case)
    answer['edge_findings'][0]['claim_match']='partial'
    paper=ground_paper(case.target,case.document,answer)
    synthesis=synthesis_answer(case,verdict='components_supported',evidence_basis='component_evidence_only')
    synthesis['edge_assessments'][0]['stance']='partially_supported'
    synthesis['corrections']=[{'edge_id':'edge-1','claim_text':'A more precise externally supported description.',
        'reason':'The original descriptor was not established.','proof_refs':deepcopy(synthesis['edge_assessments'][0]['proof_refs'])}]
    check_synthesis(case.target,[paper],synthesis)
    assert source.read_bytes()==before
    synthesis['edge_assessments'][0]['stance']='supported'
    with pytest.raises(EvidenceSelectionError):check_synthesis(case.target,[paper],synthesis)
    synthesis['edge_assessments'][0]['stance']='partially_supported'
    synthesis['corrections'][0]['proof_refs'][0]['evidence_document_id']='invented-paper'
    with pytest.raises(EvidenceSelectionError):check_synthesis(case.target,[paper],synthesis)


def test_contextual_opposition_does_not_block_applicable_support_from_another_paper(case):
    applicable=ground_paper(case.target,case.document,paper_answer(case))
    contextual=paper_answer(case,evidence_document_id='context-paper')
    contextual['edge_findings'][0].update(stance='contradicts',claim_match='context_only',conditions_match=False)
    other=ground_paper(case.target,{**case.document,'evidence_document_id':'context-paper'},contextual)
    check_synthesis(case.target,[applicable,other],synthesis_answer(case))


def test_neutral_pair_description_preserves_associations_without_temporal_language(case):
    assert ' THEN ' not in case.target['description']
    assert case.target['description'].startswith('Link 1:') and '\nLink 2:' in case.target['description']
    assert not case.target['causal_chain_validated']


def test_ranking_keeps_full_claim_details_and_neutral_endpoint_query(case):
    from backend.stages.neighbor_validation.targets import search_targets
    target=deepcopy(case.target)
    target['source_claims'][0]['claim']['claim_text']='Crop C may reduce a specific quality measure under the reported conditions.'
    facet=search_targets(target)[0]
    assert 'may reduce a specific quality measure' in facet['text']
    assert 'may reduce' not in facet['neutral_text']


def test_old_assessment_contract_remains_visible_but_cannot_enter_supported_graph(case):
    old=deepcopy(case.target);old.pop('assessment_contract')
    save_record(case.root,{'pathway_id':old['pathway_id'],'target':old,'verdict':'connection_supported'})
    records=read_records(case.root,graph=case.graph)
    assert len(records)==1 and records[0]['stale'] and not records[0]['eligible']
    assert validated_graph(case.root,{'graph':case.graph})['edges']==[]


def test_contract_refresh_does_not_queue_unrelated_old_document_pairs(case,monkeypatch):
    import backend.pipelines.neighbor_validation as pipeline
    old=deepcopy(case.target);old.pop('assessment_contract')
    save_record(case.root,{'pathway_id':old['pathway_id'],'target':old,'verdict':'queued'})
    candidates=load_candidates(case.root)
    versions={edge['id']:pipeline.signature({'mapping':{k:edge.get(k) for k in
        ('source','target','source_label','target_label')},
        'source':pipeline.source_signature(candidates[(edge['document_key'],edge['candidate_id'])])})
        for edge in case.graph['edges']}
    save_json(case.root/DIRECTORY/'selection_state.json',{'edge_versions':versions,'pending_edge_ids':[]})
    output=case.root/'Unrelated PDF';output.mkdir()
    monkeypatch.setattr(pipeline,'SemanticReranker',lambda *a:pytest.fail('Queued unrelated old-contract work'))
    result=pipeline.run_pathway_assessment(case.root,output,{'graph':case.graph},
        {'query':None,'external':None},case.settings,lambda *a:None)
    assert result['assessed_pairs']==0


def test_followup_skips_fully_resolved_facets_without_a_provider_call(case):
    client=Client(case)
    assert followup_queries(client,case.target,synthesis_answer(case))=={'queries':[]}
    assert client.calls==[]


def test_followup_article_and_passage_ranking_excludes_established_facets(case,monkeypatch):
    import backend.stages.neighbor_validation.retrieval as retrieval
    observed=[]
    def retrieve(candidate,*args,**kwargs):
        observed.extend(candidate['ranking_targets'])
        return {'evidence_documents':[]}
    monkeypatch.setattr(retrieval,'retrieve_round',retrieve)
    result=retrieval.retrieve_pathway(case.target,{'queries':[{'target_id':'edge-2','query':'neutral outcome'}]},
        None,None,case.settings,RunCache())
    assert [t['target_id'] for t in observed]==['edge-2']
    assert result['pathway_coverage']['targets_without_retained_passages']==['edge-2']


def test_new_edge_finds_all_incoming_and_outgoing_branches_without_long_chain_comparisons(case):
    edges = [{'id':i,'source':a,'target':b} for i,a,b in
        [('ab','A','B'),('bc','B','C'),('cd','C','D'),('xd','X','D'),('df','D','F'),('eg','E','G'),('de','D','E')]]
    pairs = {tuple(e['id'] for e in pair) for pair in AdjacencyIndex(edges).affected_pairs(['de'])}
    assert pairs == {('cd','de'),('xd','de'),('de','eg')}


def test_neighbor_discovery_preserves_reciprocal_feedback_without_asserting_a_whole_cycle(case):
    edges=[{'id':'ab','source':'A','target':'B'}, {'id':'ba','source':'B','target':'A'}]
    pairs={tuple(e['id'] for e in pair) for pair in AdjacencyIndex(edges).affected_pairs(['ba'])}
    assert pairs=={('ab','ba'),('ba','ab')}
    assert all(a!=b for a,b in pairs)


def test_linear_graph_selects_only_adjacent_pairs_and_reports_pair_budget(case):
    edges = [extend(case,f'n{i}',f'n{i+1}',i) for i in range(1,51)]
    graph={'edges':edges,'nodes':[]}
    targets, coverage = enumerate_targets(graph,load_candidates(case.root),limit=100)
    assert len(targets)==49
    assert all(len(t['edges'])==2 and t['whole_chain_assessment']=='not_assessed' for t in targets)
    assert coverage['complete_pair_coverage'] and not coverage['complete_pathway_coverage']
    targets, coverage = enumerate_targets(graph,load_candidates(case.root),limit=5)
    assert len(targets)==5 and coverage['has_more'] and not coverage['complete_pair_coverage']


def test_new_branch_preserves_existing_pair_id_and_does_not_reassess_ancestors(case):
    before,_=enumerate_targets(case.graph,load_candidates(case.root))
    edge=extend(case,'n2','n4',8)
    graph={**case.graph,'edges':[*case.graph['edges'],edge]}
    after,_=enumerate_targets(graph,load_candidates(case.root))
    assert len(after)==2
    assert any(t['input_signature']==before[0]['input_signature'] for t in after)
    assert {tuple(e['id'] for e in t['edges']) for t in after}=={('edge-1','edge-2'),('edge-1',edge['id'])}


def test_planning_has_two_link_queries_and_one_junction_and_reuses_saved_link_query(case):
    client=Client(case,case.settings.query_model)
    from backend.stages.neighbor_validation.targets import search_targets
    facets=search_targets(case.target)
    retained={'edge-1':{'target_id':'edge-1','query':'unchanged query','openalex_query':'unchanged query'}}
    plan=translate_pathway(client,case.target,retained)
    assert len(plan['queries'])==3
    assert all(q['target_id']!='route' for q in plan['queries'])
    assert len(client.calls[0][2]['retrieval_targets'])==2
    assert next(q for q in plan['queries'] if q['target_id']=='edge-1')['query']=='unchanged query'


def test_followup_planner_receives_local_facets_and_unresolved_assessment(case):
    from backend.stages.neighbor_validation.targets import search_targets
    assessment=synthesis_answer(case,verdict='components_supported',evidence_basis='component_evidence_only')
    assessment['edge_assessments'][1].update(stance='unresolved',proof_refs=[])
    assessment['connection_assessments'][0].update(compatibility='unresolved',proof_refs=[])
    client=Client(case,case.settings.query_model)
    result=followup_queries(client,case.target,assessment)
    payload=client.calls[0][2]
    assert payload['retrieval_targets']==search_targets(case.target)[1:]
    assert payload['assessment']==assessment
    assert {r['kind'] for r in payload['retrieval_targets']}=={'link','connection'}
    assert result['queries'][0]['target_id']==case.target['connections'][0]['connection_id']


def test_followup_with_unchanged_findings_reuses_synthesis(case,monkeypatch):
    import backend.stages.neighbor_validation.workflow as workflow
    rounds=[]
    def retrieve(*args,**kwargs):
        rounds.append(args[1])
        return fake_retrieval(case)(*args,**kwargs)
    monkeypatch.setattr(workflow,'retrieve_pathway',retrieve)
    class InitialPartial(Client):
        syntheses=0
        def chat_json(self,system,user,**kwargs):
            if 'neighbor cross-paper synthesis:' in kwargs['request_label']:
                self.syntheses+=1
                if self.syntheses==1:
                    answer=synthesis_answer(case,verdict='components_supported',evidence_basis='component_evidence_only')
                    answer['edge_assessments'][1].update(stance='unresolved',proof_refs=[])
                    answer['connection_assessments'][0].update(compatibility='unresolved',proof_refs=[])
                    return answer
            return super().chat_json(system,user,**kwargs)
    client=InitialPartial(case)
    result=assess_pathway(case.target,client,client,None,replace(case.settings,pathway_followup_papers=2),
        RunCache(),case.root/DIRECTORY/'checkpoints',lambda row:None)
    assert len(rounds)==2 and client.syntheses==1
    assert result['calls']['followup_rounds']==1
    assert result['calls']['reused_synthesis_calls']==1
    assert [r['verdict'] for r in result['assessment_history']]==['components_supported']
    assert result['verdict']=='components_supported'
    assert len(result['paper_assessments'])==1


def test_local_support_does_not_require_complete_route_demonstration(case):
    paper=ground_paper(case.target,case.document,paper_answer(case))
    answer=synthesis_answer(case)
    check_synthesis(case.target,[paper],answer)
    assert 'route_assessment' not in paper_schema(case.target,case.document)['properties']
    assert 'route_proof_refs' not in synthesis_schema(case.target,[paper])['properties']


def test_separate_papers_can_support_neighboring_claims_without_a_complete_chain_experiment(case):
    papers=[]
    for index in range(2):
        document=deepcopy(case.document)
        document['evidence_document_id']=f'paper-{index}'
        document['evidence_chunks'][0]['text']=case.document['evidence_chunks'][0]['text'].split('. ')[index]+'.'
        answer=paper_answer(case,evidence_document_id=document['evidence_document_id'])
        answer['proofs'][0]['sentence_numbers']=[1]
        for n,row in enumerate(answer['edge_findings']):
            if n!=index: row.update(stance='not_addressed',conditions_match=None,proof_ids=[])
        answer['connection_findings'][0].update(compatibility='unresolved',proof_ids=[])
        papers.append(ground_paper(case.target,document,answer))
    answer=synthesis_answer(case)
    refs=[{'evidence_document_id':f'paper-{i}','proof_id':'proof-p'} for i in range(2)]
    for i,row in enumerate(answer['edge_assessments']): row['proof_refs']=[refs[i]]
    answer['connection_assessments'][0]['proof_refs']=refs
    for role in answer['entity_roles']: role.update(resolved=False,proof_refs=[])
    check_synthesis(case.target,papers,answer)


def test_cached_claim_finding_is_reused_only_with_exact_scientific_inputs(case):
    cache=FindingCache(case.settings)
    client=Client(case)
    first=judge_paper(client,case.target,case.document,case.settings,cache)
    assert first['reused_edge_findings']==0
    second=judge_paper(client,case.target,case.document,case.settings,cache)
    assert second['reused_edge_findings']==2
    assert client.calls[-1][2]['edge_ids_to_assess']==[]
    assert all(p['proof_id'].startswith('retained-') for p in second['proofs'])
    changed=deepcopy(case.target)
    changed['source_claims'][0]['claim']['qualifiers']['condition']='Different condition'
    assert set(cache.lookup(changed,case.document))=={'edge-2'}
    changed=deepcopy(case.document)
    changed['evidence_chunks'][0]['text']+=' New evidence.'
    assert cache.lookup(case.target,changed)=={}
    assert FindingCache(replace(case.settings,thinking={'external':True})).lookup(case.target,case.document)=={}


def test_echoed_retained_proofs_are_idempotent_without_another_provider_attempt(case):
    from unittest.mock import patch
    from backend.providers.llm import OpenCodeClient
    cache=FindingCache(case.settings)
    cache.store(case.target,case.document,ground_paper(case.target,case.document,paper_answer(case)))
    model=OpenCodeClient(case.settings,case.settings.external_paper_model,task='external')
    def reply(selector,system,user,*args):
        payload=json.loads(user)
        answer=paper_answer(case,edge_findings=[],proofs=payload['retained_proofs'])
        answer['connection_findings'][0]['proof_ids']=[p['proof_id'] for p in payload['retained_proofs']]
        for key in ('pathway_id','evidence_document_id'): answer.pop(key)
        return {'content':json.dumps(answer),'finish_reason':'stop'}
    with patch.object(model._inference,'chat',side_effect=reply) as request:
        result=judge_paper(model,case.target,case.document,case.settings,cache)
    assert request.call_count==1 and result['reused_edge_findings']==2
    assert len(result['proofs'])==1
    assert result['proofs'][0]['sentence_numbers']==[1,2,3]
    assert all(row['proof_ids']==[result['proofs'][0]['proof_id']]
               for row in result['edge_findings']+result['connection_findings'])


@pytest.mark.parametrize('change',['sentences','chunk','quote'])
def test_retained_proof_label_cannot_be_reassigned_to_different_evidence(case,change):
    cache=FindingCache(case.settings)
    cache.store(case.target,case.document,ground_paper(case.target,case.document,paper_answer(case)))
    class Conflicting(Client):
        def chat_json(self,system,user,**kwargs):
            payload=json.loads(user)
            echoed=deepcopy(payload['retained_proofs'])
            if change=='sentences': echoed[0]['sentence_numbers']=[1]
            if change=='chunk': echoed[0]['chunk_id']='different-paper-chunk'
            if change=='quote': echoed[0]['quoted_span']='Invented quotation.'
            answer=paper_answer(case,edge_findings=[],proofs=echoed)
            answer['connection_findings'][0]['proof_ids']=[echoed[0]['proof_id']]
            return answer
    with pytest.raises(EvidenceSelectionError,match='different paper-bound evidence') as caught:
        judge_paper(Conflicting(case),case.target,case.document,case.settings,cache)
    assert caught.value.review_response['proofs']
    assert next(iter(cache.lookup(case.target,case.document).values()))['proofs'][0]['sentence_numbers']==[1,2,3]


def test_duplicate_new_proof_labels_remain_ambiguous_and_rejected(case):
    class Duplicate(Client):
        def chat_json(self,*args,**kwargs):
            answer=paper_answer(case)
            answer['proofs'].append({**answer['proofs'][0],'sentence_numbers':[2]})
            return answer
    with pytest.raises(EvidenceSelectionError,match='unique'):
        judge_paper(Duplicate(case),case.target,case.document,case.settings)


def test_invalid_paper_proof_is_held_while_other_papers_complete_and_resume_retries_only_the_hold(case,monkeypatch):
    import backend.stages.neighbor_validation.workflow as workflow
    bad={**deepcopy(case.document),'evidence_document_id':'paper-review','doi':'10.example/review'}
    def retrieve(*args,**kwargs):
        for document in [case.document,bad]: args[-1](document)
        return {**prepared(case),'evidence_documents':[case.document,bad]}
    monkeypatch.setattr(workflow,'retrieve_pathway',retrieve)
    calls=[]
    def judge(client,target,document,settings,finding_cache):
        identifier=document['evidence_document_id'];calls.append(identifier)
        if identifier=='paper-review' and calls.count(identifier)==1:
            exc=EvidenceSelectionError('Selected sentence does not exist')
            exc.review_response={'proofs':[{'sentence_numbers':[99]}]}
            raise exc
        return ground_paper(target,document,paper_answer(case,evidence_document_id=identifier))
    monkeypatch.setattr(workflow,'judge_paper',judge)
    directory=case.root/DIRECTORY/'checkpoints'
    records=[]
    first=assess_pathway(case.target,Client(case),Client(case),None,case.settings,RunCache(),directory,records.append)
    assert first['verdict']=='assessment_incomplete' and not first['local_connection_assessed']
    assert len(first['paper_assessments'])==1 and len(first['paper_assessment_errors'])==1
    assert first['paper_assessment_errors'][0]['review_response']['proofs']
    monkeypatch.setattr(workflow,'retrieve_pathway',lambda *a,**kw:pytest.fail('Repeated completed retrieval'))
    resumed=assess_pathway(case.target,Client(case),Client(case),None,case.settings,RunCache(),directory,records.append)
    assert resumed['verdict']=='connection_supported' and resumed['local_connection_assessed']
    assert len(resumed['paper_assessments'])==2 and resumed['paper_assessment_errors']==[]
    assert calls.count('paper-p')==1 and calls.count('paper-review')==2


@pytest.mark.parametrize('old_fails',[False,True])
def test_slow_initial_paper_packet_cannot_replace_its_completed_expansion(case,monkeypatch,old_fails):
    from threading import Event
    import backend.stages.neighbor_validation.workflow as workflow
    expanded=deepcopy(case.document)
    expanded['evidence_chunks'].append({'chunk_id':'extra-chunk','text':'An additional definition.'})
    published=Event()
    original=workflow._PaperAssessmentSession._publish_finding
    def publish(self,identifier,finding):
        original(self,identifier,finding)
        if finding['finding']=='Expanded packet':published.set()
    monkeypatch.setattr(workflow._PaperAssessmentSession,'_publish_finding',publish)
    def retrieve(*args,**kwargs):
        args[-1](case.document);args[-1](expanded)
        return {**prepared(case),'evidence_documents':[expanded]}
    monkeypatch.setattr(workflow,'retrieve_pathway',retrieve)
    def judge(client,target,document,settings,finding_cache):
        if len(document['evidence_chunks'])==1:
            assert published.wait(5)
            if old_fails:raise EvidenceSelectionError('Old superseded proposal failed')
            text='Initial packet'
        else:text='Expanded packet'
        return ground_paper(target,document,paper_answer(case,finding=text))
    monkeypatch.setattr(workflow,'judge_paper',judge)
    result=assess_pathway(case.target,Client(case),Client(case),None,case.settings,RunCache(),
        case.root/DIRECTORY/'checkpoints',lambda row:None)
    assert result['verdict']=='connection_supported'
    assert result['paper_assessments'][0]['finding']=='Expanded packet'
    assert result['paper_assessment_errors']==[]
    assert len(result['evidence_documents'][0]['evidence_chunks'])==2


def test_synthesis_with_invalid_proof_is_incomplete_instead_of_aborting_other_pairs(case,monkeypatch):
    import backend.stages.neighbor_validation.workflow as workflow
    monkeypatch.setattr(workflow,'retrieve_pathway',fake_retrieval(case))
    def synthesize(*args):raise EvidenceSelectionError('Unknown proof in synthesis')
    monkeypatch.setattr(workflow,'synthesize',synthesize)
    result=assess_pathway(case.target,Client(case),Client(case),None,case.settings,RunCache(),
        case.root/DIRECTORY/'checkpoints',lambda row:None)
    assert result['verdict']=='assessment_incomplete' and not result['local_connection_assessed']
    assert result['paper_assessments'] and result['error']=='Unknown proof in synthesis'


def test_provider_outage_still_stops_instead_of_becoming_scientific_non_support(case,monkeypatch):
    import backend.stages.neighbor_validation.workflow as workflow
    monkeypatch.setattr(workflow,'retrieve_pathway',fake_retrieval(case))
    def unavailable(*args):raise LLMServiceError('Provider unavailable')
    monkeypatch.setattr(workflow,'judge_paper',unavailable)
    with pytest.raises(LLMServiceError,match='Provider unavailable'):
        assess_pathway(case.target,Client(case),Client(case),None,case.settings,RunCache(),
            case.root/DIRECTORY/'checkpoints',lambda row:None)


def test_callback_checkpoint_failure_is_propagated_on_the_owner(case,monkeypatch):
    import backend.stages.neighbor_validation.workflow as workflow
    monkeypatch.setattr(workflow,'retrieve_pathway',fake_retrieval(case))
    def checkpoint(row):
        if row['paper_assessments']:raise OSError('Checkpoint storage unavailable')
    with pytest.raises(OSError,match='Checkpoint storage unavailable'):
        assess_pathway(case.target,Client(case),Client(case),None,case.settings,RunCache(),
            case.root/DIRECTORY/'checkpoints',checkpoint)


def test_cached_findings_cannot_move_proof_to_another_paper_or_changed_mapping(case):
    cache=FindingCache(case.settings)
    judge_paper(Client(case),case.target,case.document,case.settings,cache)
    document={**case.document,'evidence_document_id':'other-paper'}
    assert cache.lookup(case.target,document)=={}
    target=deepcopy(case.target)
    target['edges'][0]['source']='corrected-node'
    assert set(cache.lookup(target,case.document))=={'edge-2'}


def test_query_specific_ranking_decorations_do_not_invalidate_unchanged_scientific_evidence(case):
    cache=FindingCache(case.settings)
    judge_paper(Client(case),case.target,case.document,case.settings,cache)
    reranked=deepcopy(case.document)
    reranked['evidence_chunks'][0].update(dense_score=0.73,retrieval_target_ids=['another-junction'])
    assert set(cache.lookup(case.target,reranked))=={'edge-1','edge-2'}


def test_explicit_redo_bypasses_prior_claim_findings_but_its_resume_can_reuse_them(case):
    cache=FindingCache(case.settings)
    judge_paper(Client(case),case.target,case.document,case.settings,cache)
    redo=FindingCache(case.settings,assessment_id='new-assessment')
    # Even shared storage would use another namespace, not inherited judgements.
    assert redo._key(case.target['source_claims'][0],case.target['edges'][0],case.document)!=cache._key(
        case.target['source_claims'][0],case.target['edges'][0],case.document)


def test_unknown_and_nonlocal_sentence_selections_fail_with_paper_traceability(case):
    answer=paper_answer(case)
    answer['proofs'][0]['sentence_numbers']=[99]
    with pytest.raises(EvidenceSelectionError): ground_paper(case.target,case.document,answer)
    answer=paper_answer(case,evidence_document_id='invented-paper')
    with pytest.raises(EvidenceSelectionError): ground_paper(case.target,case.document,answer)


def test_contextual_condition_mismatch_cannot_be_promoted_to_local_support(case):
    answer=paper_answer(case)
    answer['edge_findings'][0]['conditions_match']=False
    paper=ground_paper(case.target,case.document,answer)
    with pytest.raises(EvidenceSelectionError):check_synthesis(case.target,[paper],synthesis_answer(case))


def test_unresolved_connection_still_cannot_cite_invented_proof(case):
    paper=ground_paper(case.target,case.document,paper_answer(case))
    answer=synthesis_answer(case,verdict='components_supported',evidence_basis='component_evidence_only')
    answer['connection_assessments'][0].update(compatibility='unresolved',
        proof_refs=[{'evidence_document_id':'another-paper','proof_id':'invented'}])
    with pytest.raises(EvidenceSelectionError,match='moves it to another paper'):
        check_synthesis(case.target,[paper],answer)


def test_completed_local_pair_resumes_without_retrieval_or_model_calls(case,monkeypatch):
    import backend.stages.neighbor_validation.workflow as workflow
    monkeypatch.setattr(workflow,'retrieve_pathway',fake_retrieval(case))
    client=Client(case)
    directory=case.root/DIRECTORY/'checkpoints'
    records=[]
    result=assess_pathway(case.target,client,client,None,case.settings,RunCache(),directory,records.append)
    assert result['verdict']=='connection_supported'
    assert result['local_connection_assessed'] and result['whole_pathway_assessed'] is False
    before=len(client.calls)
    monkeypatch.setattr(workflow,'retrieve_pathway',lambda *a,**kw:pytest.fail('Repeated retrieval'))
    assert assess_pathway(case.target,client,client,None,case.settings,RunCache(),directory,records.append)==result
    assert len(client.calls)==before


def test_pending_retrieval_retains_findings_without_admitting_local_support(case,monkeypatch):
    import backend.stages.neighbor_validation.workflow as workflow
    monkeypatch.setattr(workflow,'retrieve_pathway',fake_retrieval(case,True))
    result=assess_pathway(case.target,Client(case),Client(case),None,case.settings,RunCache(),
                         case.root/DIRECTORY/'checkpoints',lambda r:None)
    assert result['verdict']=='assessment_incomplete'
    assert result['paper_assessments'] and result['assessment']['verdict']=='connection_supported'
    assert not result['local_connection_assessed']


def test_corrections_invalidate_only_affected_pairs_and_never_make_union_causal(case):
    from backend.stages.neighbor_validation.proof_audit import audit_support
    assessment = synthesis_answer(case)
    papers = [ground_paper(case.target,case.document,paper_answer(case))]
    answer={'pathway_id':case.target['pathway_id'],'target':case.target,'verdict':'connection_supported',
            'assessment':assessment,'paper_assessments':papers,
            'proof_audit':audit_support(Client(case),case.target,papers,assessment,case.settings)}
    save_record(case.root,answer)
    graph=validated_graph(case.root,{'graph':case.graph})
    assert len(graph['edges'])==2 and graph['whole_chain_assessment']=='not_assessed'
    assert graph['graph_union_is_not_a_validated_pathway']
    assert all(e['causal_chain_validated'] is False for e in graph['edges'])
    candidates=read_jsonl(case.root/'Document 1'/'candidates.jsonl')
    candidates[0]['qualifiers']['condition']='Changed dose'
    save_rows(case.root/'Document 1'/'candidates.jsonl',candidates)
    assert read_records(case.root,graph=case.graph)[0]['stale']
    assert not validated_graph(case.root,{'graph':case.graph})['edges']


def test_old_whole_route_is_read_only_history_and_not_local_support(case):
    from backend.stages.neighbor_validation.records import historical_records
    old={**case.target,'policy':'whole-pathway-v1'}
    save_rows(case.root/'.pathway_validation'/'results.jsonl',
              [{'pathway_id':'old','target':old,'verdict':'pathway_supported'}])
    rows=historical_records(case.root,case.graph)
    assert len(rows)==1 and rows[0]['historical'] and not rows[0]['eligible']
    assert rows[0]['assessment_scope']=='whole_pathway'
    assert read_records(case.root)==[]


def test_schema_allows_zero_new_edge_findings_when_reusing_both(case):
    schema=paper_schema(case.target,case.document,[])
    answer=paper_answer(case,edge_findings=[])
    answer={k:v for k,v in answer.items() if k not in ['pathway_id','evidence_document_id']}
    validate_schema(answer,schema)


def test_runtime_has_pair_budget_and_no_nli_or_global_validation_depth():
    from dataclasses import fields
    from backend.api.contracts import RunOptions
    from backend.config.settings import APPLICATION_VERSION
    assert 'neighbor_max_pairs' in RunOptions.model_fields
    assert not {'pathway_max_paths','pathway_max_hops'} & RunOptions.model_fields.keys()
    assert not {'enable_nli','nli_model','nli_device'} & {f.name for f in fields(Settings)}
    assert APPLICATION_VERSION=='neighbor-validation-v50'


def test_reuse_signature_tracks_scientific_settings_but_not_queue_budget(case):
    from backend.stages.neighbor_validation.workflow import config_signature
    baseline=config_signature(case.target,case.settings)
    assert config_signature(case.target,replace(case.settings,neighbor_max_pairs=1))==baseline
    assert config_signature(case.target,replace(case.settings,cross_encoder_candidates=21))!=baseline
    assert config_signature(case.target,replace(case.settings,external_min_output_budget=32768))!=baseline


def test_pair_workers_overlap_and_resume_skips_completed_pairs_without_loading_models(case,monkeypatch):
    from threading import Barrier, Lock
    import backend.pipelines.neighbor_validation as pipeline
    edge=extend(case,'n2','n4',23)
    graph={**case.graph,'edges':[*case.graph['edges'],edge],
           'nodes':[*case.graph['nodes'],{'id':'n4','label':'Outcome F','type':'PlantTrait'}]}
    settings=replace(case.settings,neighbor_workers=2)
    barrier=Barrier(2)
    lock=Lock()
    observed={'active':0,'maximum':0,'calls':0}
    class Ranker:
        embedding_cache_hits=0
        def __init__(self,*args):pass
        def warmup(self):pass
    monkeypatch.setattr(pipeline,'SemanticReranker',Ranker)
    def assess(target,query,external,ranker,settings,cache,directory,checkpoint,**kwargs):
        with lock:
            observed['active']+=1
            observed['calls']+=1
            observed['maximum']=max(observed['maximum'],observed['active'])
        barrier.wait(timeout=5)
        record={'pathway_id':target['pathway_id'],'target':target,'policy':POLICY,
            'validation_signature':pipeline.config_signature(target,settings),
            'verdict':'unresolved','local_connection_assessed':True,
            'whole_pathway_assessed':False,'whole_chain_assessment':'not_assessed'}
        checkpoint(record)
        with lock: observed['active']-=1
        return record
    monkeypatch.setattr(pipeline,'assess_pathway',assess)
    provisional={'graph':graph}
    result=pipeline.run_pathway_assessment(case.root,case.root/'Document 1',provisional,
        {'query':None,'external':None},settings,lambda *a:None)
    assert observed['maximum']==2 and observed['calls']==2
    assert result['assessed_pairs']==2 and result['assessed_pathways']==0
    monkeypatch.setattr(pipeline,'SemanticReranker',lambda *a:pytest.fail('Loaded models for completed work'))
    resumed=pipeline.run_pathway_assessment(case.root,case.root/'Document 1',provisional,
        {'query':None,'external':None},settings,lambda *a:None)
    assert resumed['reused_pairs']==2 and observed['calls']==2


def test_budgeted_resume_advances_past_completed_pairs(case,monkeypatch):
    import backend.pipelines.neighbor_validation as pipeline
    branch=extend(case,'n2','n4',24)
    graph={**case.graph,'edges':[*case.graph['edges'],branch]}
    calls=[]
    class Ranker:
        embedding_cache_hits=0
        def __init__(self,*args):pass
        def warmup(self):pass
    monkeypatch.setattr(pipeline,'SemanticReranker',Ranker)
    def assess(target,query,external,ranker,settings,cache,directory,checkpoint,**kwargs):
        calls.append(target['pathway_id'])
        row={'pathway_id':target['pathway_id'],'target':target,'policy':POLICY,
             'validation_signature':pipeline.config_signature(target,settings),
             'verdict':'unresolved','local_connection_assessed':True}
        checkpoint(row)
        return row
    monkeypatch.setattr(pipeline,'assess_pathway',assess)
    settings=replace(case.settings,neighbor_max_pairs=1)
    def run():
        return pipeline.run_pathway_assessment(case.root,case.root/'Document 1',{'graph':graph},
            {'query':None,'external':None},settings,lambda *a:None)
    first=run()
    assert first['coverage']['has_more'] and len(calls)==1
    second=run()
    assert not second['coverage']['has_more'] and len(set(calls))==2
    assert second['reused_pairs']==1
    assert run()['reused_pairs']==2 and len(calls)==2


def test_interrupted_other_document_pairs_remain_selected_after_version_checkpoint(case,monkeypatch):
    import backend.pipelines.neighbor_validation as pipeline
    # Both edges belong to other PDFs; a previous selection already recorded
    # their versions before its provider call was interrupted.
    output=case.root/'Unrelated PDF'
    output.mkdir()
    candidates=load_candidates(case.root)
    versions={edge['id']:pipeline.signature({'mapping':{k:edge.get(k) for k in
        ('source','target','source_label','target_label')},
        'source':pipeline.source_signature(candidates[(edge['document_key'],edge['candidate_id'])])})
        for edge in case.graph['edges']}
    save_json(case.root/DIRECTORY/'selection_state.json',{'edge_versions':versions,'pending_edge_ids':[]})
    save_record(case.root,{'pathway_id':case.target['pathway_id'],'target':case.target,'policy':POLICY,
        'validation_signature':pipeline.config_signature(case.target,case.settings),'verdict':'queued'})
    class Ranker:
        embedding_cache_hits=0
        def __init__(self,*args):pass
        def warmup(self):pass
    monkeypatch.setattr(pipeline,'SemanticReranker',Ranker)
    observed=[]
    def assess(target,query,external,ranker,settings,cache,directory,checkpoint,**kwargs):
        observed.append(target['pathway_id'])
        row={'pathway_id':target['pathway_id'],'target':target,'policy':POLICY,
            'validation_signature':pipeline.config_signature(target,settings),'verdict':'unresolved'}
        checkpoint(row)
        return row
    monkeypatch.setattr(pipeline,'assess_pathway',assess)
    result=pipeline.run_pathway_assessment(case.root,output,{'graph':case.graph},
        {'query':None,'external':None},case.settings,lambda *a:None)
    assert observed==[case.target['pathway_id']] and result['assessed_pairs']==1


def test_mapping_change_discovers_new_pairs_in_other_documents(case,monkeypatch):
    import backend.pipelines.neighbor_validation as pipeline
    output=case.root/'Unrelated PDF'
    output.mkdir()
    candidates=load_candidates(case.root)
    # Previously the downstream edge had another identity, so no pair existed.
    old=deepcopy(case.graph['edges'])
    old[1]['source']='previous-distinct-entity'
    versions={edge['id']:pipeline.signature({'mapping':{k:edge.get(k) for k in
        ('source','target','source_label','target_label')},
        'source':pipeline.source_signature(candidates[(edge['document_key'],edge['candidate_id'])])})
        for edge in old}
    save_json(case.root/DIRECTORY/'selection_state.json',{'edge_versions':versions,'pending_edge_ids':[]})
    class Ranker:
        embedding_cache_hits=0
        def __init__(self,*args):pass
        def warmup(self):pass
    monkeypatch.setattr(pipeline,'SemanticReranker',Ranker)
    observed=[]
    def assess(target,query,external,ranker,settings,cache,directory,checkpoint,**kwargs):
        observed.append(target['pathway_id'])
        row={'pathway_id':target['pathway_id'],'target':target,'policy':POLICY,
            'validation_signature':pipeline.config_signature(target,settings),'verdict':'unresolved'}
        checkpoint(row)
        return row
    monkeypatch.setattr(pipeline,'assess_pathway',assess)
    pipeline.run_pathway_assessment(case.root,output,{'graph':case.graph},
        {'query':None,'external':None},case.settings,lambda *a:None)
    assert observed==[case.target['pathway_id']]


