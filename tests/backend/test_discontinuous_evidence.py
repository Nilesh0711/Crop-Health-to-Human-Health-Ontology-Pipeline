"""A floating table must not force silent source splicing or abort valid extraction."""
from copy import deepcopy
from unittest.mock import patch
import json

from backend.providers.response_repair import validate_schema
from backend.stages.claim_extraction.targeting import check_target_evidence
from backend.stages.claim_extraction.contract import EXTRACTION_RESPONSE_SCHEMA
from backend.stages.claim_extraction.extractor import extract_claims, evidence_span_from_item
from backend.stages.source_validation.checker import validate_source_claim
from tests.backend.test_extraction_targets import row, packet, claim
from tests.backend.test_source_validation import FakeClient


def fixture():
    rows = [row(1,text='Wheat grains may contain cadmium under disease Z infection.'),
            row(2,text='Table 1. Separate measurement data.'),
            row(3,text='Cadmium was absent from untreated wheat.'),
            row(4,text='The finding was restricted to disease Z infection.')]
    rows[-1]['page'] = 3
    return rows


def test_nonadjacent_source_ids_keep_exact_fragments_and_every_intervening_sentence(tmp_path):
    rows = fixture()
    proposal = claim([1,4])
    class Client:
        def chat_json(self, system, user, **options):
            value = {'claims':[deepcopy(proposal)]}
            validate_schema(value,options['json_schema'])
            options['response_check'](value)
            return value
    pdf = tmp_path/'fixture.pdf'
    pdf.write_bytes(b'parsed fixture')
    with patch('backend.stages.claim_extraction.extractor.source_paper_title',return_value='Fixture'):
        candidates, rejected, _ = extract_claims(pdf,Client(),0,lambda *args:None,packets=[packet(rows)],
            bibliography=[],source_identifiers={'source_doi':'fixture'},extraction_mode='target_context')
    assert len(candidates) == 1 and not rejected
    candidate = candidates[0]
    assert candidate['source_evidence_sentence_numbers'] == [1,4]
    assert candidate['source_evidence_is_contiguous'] is False
    assert [f['text'] for f in candidate['source_evidence_fragments']] == [rows[0]['text'],rows[3]['text']]
    assert [f['page'] for f in candidate['source_evidence_fragments']] == [1,3]
    assert [f['text'] for f in candidate['source_intervening_context']] == [rows[1]['text'],rows[2]['text']]
    assert '[Non-adjacent source fragment]' in candidate['source_evidence_span']
    assert 'Cadmium was absent' in candidate['source_local_context']
    assert candidate['source_evidence_records'][0]['source_intervening_context'] == candidate['source_intervening_context']
    candidate['candidate_id']='fixture'
    judge = FakeClient({'decision':'needs_review','rationale':'Intervening conditions need review.'})
    with patch.object(judge,'chat_json',wraps=judge.chat_json) as called:
        result=validate_source_claim(candidate,judge)
    payload=json.loads(called.call_args.args[1])
    assert payload['source_intervening_context'] == candidate['source_intervening_context']
    assert payload['source_evidence_fragments'] == candidate['source_evidence_fragments']
    assert result['decision']=='needs_review'  # No local promotion or scientific judgement.


def test_existing_contiguous_evidence_keeps_original_text_and_invalid_ids_still_fail():
    rows=fixture()
    resolved=evidence_span_from_item({'evidence_sentence_numbers':[1,2]},rows)
    assert resolved[2] == rows[0]['text']+' '+rows[1]['text']
    for invalid in ([0],[5],[True],[2,1],[1,1],[1,2,3,4]):
        assert evidence_span_from_item({'evidence_sentence_numbers':invalid},rows) is None
    check_target_evidence({'claims':[{'evidence_sentence_numbers':[1,4]}]},[1,2,3,4])


def test_source_proof_cannot_span_a_gap_marker():
    rows=fixture()
    resolved=evidence_span_from_item({'evidence_sentence_numbers':[1,4]},rows)
    assert resolved[2] != ' '.join([rows[0]['text'],rows[3]['text']])
    candidate=claim([1,4])
    candidate.update(candidate_id='gap-test',source_evidence_sentence_numbers=[1,4],source_evidence_span=resolved[2],
        source_evidence_is_contiguous=False,source_evidence_fragments=[{'text':r['text']} for r in [rows[0],rows[3]]])
    response={'decision':'entailed','relation_match':True,'direction_match':True,'certainty_match':True,
              'proof':{'subject':'Wheat grains','object':'cadmium','relation':'Non-adjacent source fragment'}}
    result=validate_source_claim(candidate,FakeClient(response))
    assert result['decision']=='needs_review'
    assert 'relation proof is missing from the selected evidence' in result['reasons']
