"""Quote-only audit and arithmetic contracts; no live scientific gold."""
from copy import deepcopy
import json
from decimal import Decimal

import pytest

from backend.providers.structured_output import EvidenceSelectionError
from backend.shared.cache import RunCache
from backend.stages.external_validation.arithmetic import calculate_from_proof
from backend.stages.neighbor_validation.proof_audit import audit_support, followup_assessment
from backend.stages.neighbor_validation.judgement import ground_paper
from backend.stages.neighbor_validation.records import read_records, save_record, DIRECTORY
from backend.stages.neighbor_validation.workflow import assess_pathway
from tests.external.test_neighbor_validation import case, Client, synthesis_answer, paper_answer, fake_retrieval


@pytest.mark.parametrize('operation,operands,expected', [
    ('ratio_percent',['50','200'],'25'), ('change_percent',['200','150'],'-25'),
    ('sum_ratio_percent',['20','30','200'],'25')])
def test_arithmetic_uses_literal_numbers_but_does_not_judge_scientific_comparability(operation,operands,expected):
    proof = {'p':{'quoted_span':'Counts were 200 and 150; subgroups were 50, 20 and 30.'}}
    result = calculate_from_proof({'proof_id':'p','operation':operation,'operands':operands},proof)
    assert Decimal(result['value_percent'])==Decimal(expected)
    assert result['units_and_scientific_comparability_checked'] is False


@pytest.mark.parametrize('operands', [['40','200'],['5+5','200'],['1,2','200'],['50','0']])
def test_arithmetic_never_invents_operands_or_evaluates_expressions(operands):
    with pytest.raises(ValueError):
        calculate_from_proof({'proof_id':'p','operation':'ratio_percent','operands':operands},
                             {'p':{'quoted_span':'Values 50 and 200. A zero value 0 was excluded; token 1,2 is ambiguous.'}})


@pytest.mark.parametrize('quote', ['50,000 and 200.', '-50 and 200.', '50.4 and 200.'])
def test_arithmetic_does_not_match_only_part_of_a_signed_or_formatted_number(quote):
    with pytest.raises(ValueError,match='literal number'):
        calculate_from_proof({'proof_id':'p','operation':'ratio_percent','operands':['50','200']},
                             {'p':{'quoted_span':quote}})


@pytest.mark.parametrize('word', ['eight','Eight','EIGHT','eIgHt'])
def test_number_word_case_does_not_change_arithmetic_or_stored_proof(word):
    proof={'p':{'quoted_span':'Eight outcomes were observed among ten samples.'}}
    original=deepcopy(proof)
    calculation={'proof_id':'p','operation':'ratio_percent','operands':[word,'TEN']}
    result=calculate_from_proof(calculation,proof)
    assert Decimal(result['value_percent'])==Decimal('80')
    assert result['operands']==[word,'TEN']
    assert proof==original


@pytest.mark.parametrize('quote', ['An eightfold change among ten samples.', 'The weight of ten samples.'])
def test_number_words_still_require_complete_literal_word_boundaries(quote):
    with pytest.raises(ValueError,match='literal number'):
        calculate_from_proof({'proof_id':'p','operation':'ratio_percent','operands':['eight','ten']},
                             {'p':{'quoted_span':quote}})


@pytest.mark.parametrize('field',['edge_checks','connection_checks'])
def test_not_established_can_omit_references_without_passing_audit(case,field):
    papers=[ground_paper(case.target,case.document,paper_answer(case))]
    assessment=synthesis_answer(case)
    client=Client(case)
    original=audit_support(client,case.target,papers,assessment,case.settings)
    held=deepcopy(original)
    held[field][0].update(decision='not_established',proof_refs=[],reason='Admitted quotations do not establish this finding.')
    class MissingProof(Client):
        def chat_json(self,*args,**kwargs):
            return {key:deepcopy(held[key]) for key in ('edge_checks','connection_checks')}
    # Validate both a new model response and the retained checkpoint path.
    for retained in (None,held):
        result=audit_support(MissingProof(case),case.target,papers,assessment,case.settings,retained=retained)
        assert result['passed'] is False
        assert result[field][0]['proof_refs']==[]
    assert original[field][0]['proof_refs']
    held[field][0]['proof_refs']=[{'evidence_document_id':'invented-paper','proof_id':'invented-proof'}]
    with pytest.raises(EvidenceSelectionError,match='moves evidence'):
        audit_support(client,case.target,papers,assessment,case.settings,retained=held)


@pytest.mark.parametrize('field,decision',[
    ('edge_checks','exact'),('edge_checks','partial'),('edge_checks','conflicting'),
    ('connection_checks','compatible'),('connection_checks','incompatible')])
def test_other_audit_decisions_still_require_bound_references(case,field,decision):
    papers=[ground_paper(case.target,case.document,paper_answer(case))]
    assessment=synthesis_answer(case)
    client=Client(case)
    audit=audit_support(client,case.target,papers,assessment,case.settings)
    audit[field][0].update(decision=decision,proof_refs=[])
    with pytest.raises(EvidenceSelectionError,match='requires references'):
        audit_support(client,case.target,papers,assessment,case.settings,retained=audit)


def test_audit_sees_external_quotes_and_fixed_claims_but_not_originating_context(case):
    case.target['source_claims'][0]['claim']['source_local_context']='ORIGIN_ONLY_SENTINEL'
    papers=[ground_paper(case.target,case.document,paper_answer(case))]
    client=Client(case)
    audit=audit_support(client,case.target,papers,synthesis_answer(case),case.settings)
    assert audit['passed']
    assert 'ORIGIN_ONLY_SENTINEL' not in json.dumps(client.calls[-1][2])
    assert 'original_pdf_context_only' not in client.calls[-1][2]
    bad=deepcopy(audit)
    bad['edge_checks'][0]['proof_refs'][0]['evidence_document_id']='another-paper'
    with pytest.raises(EvidenceSelectionError,match='moves evidence'):
        audit_support(client,case.target,papers,synthesis_answer(case),case.settings,retained=bad)


@pytest.mark.parametrize('decision',['partial','not_established'])
def test_quote_audit_hold_keeps_original_synthesis_and_is_reused_without_repeated_calls(case,monkeypatch,decision):
    monkeypatch.setattr('backend.stages.neighbor_validation.workflow.retrieve_pathway',fake_retrieval(case))
    class Cautious(Client):
        def for_model(self,model): return Cautious(self.case,model,self.calls)
        def chat_json(self,system,user,**kwargs):
            answer=super().chat_json(system,user,**kwargs)
            if 'neighbor proof audit:' in kwargs['request_label']:
                answer['edge_checks'][0].update(decision=decision,reason='Extent qualifier is not established by admitted quotes.')
                if decision=='not_established':
                    answer['edge_checks'][0]['proof_refs']=[]
            return answer
    client=Cautious(case)
    result=assess_pathway(case.target,client,client,None,case.settings,RunCache(),case.root/DIRECTORY,
                         lambda record:save_record(case.root,record))
    assert result['verdict']=='assessment_incomplete'
    assert result['assessment']['verdict']=='connection_supported'
    assert result['proof_audit']['passed'] is False
    assert not read_records(case.root,graph=case.graph)[0]['eligible']
    before=len(client.calls)
    resumed=assess_pathway(case.target,client,client,None,case.settings,RunCache(),case.root/DIRECTORY,
                          lambda record:save_record(case.root,record))
    assert resumed['verdict']=='assessment_incomplete'
    assert len(client.calls)==before
    assert resumed['calls']['reused_proof_audits']==1
    planned=followup_assessment(result['assessment'],result['proof_audit'])
    assert planned['edge_assessments'][0]['stance']=='unresolved'
    assert result['assessment']['edge_assessments'][0]['stance']=='supported'


def test_unaudited_supported_record_cannot_enter_the_graph(case):
    save_record(case.root,{'pathway_id':case.target['pathway_id'],'target':case.target,'verdict':'connection_supported'})
    assert read_records(case.root,graph=case.graph)[0]['eligible'] is False


def test_synthesis_and_quote_audit_use_muse_while_paper_judgement_stays_deepseek(case,monkeypatch):
    monkeypatch.setattr('backend.stages.neighbor_validation.workflow.retrieve_pathway',fake_retrieval(case))
    client=Client(case)
    result=assess_pathway(case.target,client,client,None,case.settings,RunCache(),case.root/DIRECTORY,
                         lambda row:save_record(case.root,row))
    assert result['verdict']=='connection_supported'
    assert case.settings.external_model=='opencode-go/muse-spark-1.3-contributor'
    for model,label,_ in client.calls:
        if 'neighbor paper external:' in label:
            assert model=='opencode-go/deepseek-v4.1-flash'
        if 'neighbor cross-paper synthesis:' in label or 'neighbor proof audit:' in label:
            assert model=='opencode-go/muse-spark-1.3-contributor'
    assert any('neighbor cross-paper synthesis:' in row[1] for row in client.calls)
    assert any('neighbor proof audit:' in row[1] for row in client.calls)


def test_changing_proof_after_a_passed_audit_revokes_admission(case):
    papers=[ground_paper(case.target,case.document,paper_answer(case))]
    assessment=synthesis_answer(case)
    audit=audit_support(Client(case),case.target,papers,assessment,case.settings)
    record={'pathway_id':case.target['pathway_id'],'target':case.target,'verdict':'connection_supported',
            'paper_assessments':papers,'assessment':assessment,'proof_audit':audit}
    save_record(case.root,record)
    assert read_records(case.root,graph=case.graph)[0]['eligible']
    papers[0]['proofs'][0]['quoted_span']='Changed evidence after the audit.'
    save_record(case.root,record)
    assert not read_records(case.root,graph=case.graph)[0]['eligible']


def test_pipeline_does_not_skip_a_completed_support_record_without_a_valid_audit(case,monkeypatch):
    import backend.pipelines.neighbor_validation as pipeline
    save_record(case.root,{'pathway_id':case.target['pathway_id'],'target':case.target,
        'verdict':'connection_supported','validation_signature':pipeline.config_signature(case.target,case.settings)})
    class Ranker:
        embedding_cache_hits=0
        def __init__(self,*args): pass
        def warmup(self): pass
    monkeypatch.setattr(pipeline,'SemanticReranker',Ranker)
    calls=[]
    def assess(target,*args,**kwargs):
        calls.append(target['pathway_id'])
        row={'pathway_id':target['pathway_id'],'target':target,'verdict':'unresolved'}
        args[6](row)
        return row
    monkeypatch.setattr(pipeline,'assess_pathway',assess)
    pipeline.run_pathway_assessment(case.root,case.root/'Document 1',{'graph':case.graph},
        {'query':None,'external':None},case.settings,lambda *args:None)
    assert calls==[case.target['pathway_id']]
