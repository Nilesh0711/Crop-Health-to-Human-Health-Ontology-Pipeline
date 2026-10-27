"""Context-preserving extraction and honest throughput regression checks."""

import copy
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from backend.domain.claim_context import claim_description, local_evidence_context
from backend.domain.ontology import SCOPE_POLICY, schema_error
from backend.stages.claim_extraction.extractor import compact_literal_entity_anchors, extract_claims
from backend.stages.pdf_parsing.packets import layout_heading_candidates
from backend.stages.source_validation.checker import validate_source_claim
from backend.config.settings import Settings
from backend.providers.llm import OpenCodeClient, format_request_metrics, model_reasoning_policy
from tests.regression.test_pipeline_v24 import ReplyClient, alignment, source_claim, source_reply


def test_context_is_sentence_bounded_and_never_crosses_a_section_boundary():
    sentences = [{"text": f"Sentence {index}.", "section_heading": "Earlier" if index < 2 else "Local"}
                 for index in range(8)]
    context = local_evidence_context(sentences, [4])
    assert "[LOCAL SECTION HEADING] Local" in context
    assert "Sentence 1." not in context
    assert "Sentence 2." in context and "Sentence 5." in context
    assert "Sentence 6." not in context


def test_plain_and_oversized_descriptions_get_a_neutral_not_invented_fallback():
    candidate = source_claim()
    text, kind = claim_description(candidate)
    assert kind == "structured_rendering" and "organic farming" in text
    text, kind = claim_description({**candidate, "claim_text": "word " * 81})
    assert kind == "structured_rendering"
    text, kind = claim_description({**candidate, "claim_text": "Organic farming reduces soil pollution."})
    assert kind == "extracted" and text.endswith("pollution.")


def test_contextual_identity_does_not_require_the_complete_canonical_phrase_in_one_quote():
    span = "Another feature of disease Z is that it affects the cooking quality of crop R."
    candidate = {**source_claim(), "subject": "disease Z of crop R", "subject_type": "PlantDisease",
                 "relation": "affects", "object": "cooking quality of crop R", "object_type": "PlantTrait",
                 "subject_source_mention": "disease Z", "object_source_mention": "the cooking quality of crop R",
                 "relation_trigger": "affects", "source_evidence_span": span,
                 "source_evidence_section_heading": "Disease Z of crop R in region S",
                 "source_context_packet": span}
    reply = {**source_reply(), "proof": {"subject": "disease Z", "relation": "affects", "object": "the cooking quality of crop R"},
             "entity_alignment": {
                 "subject": alignment(candidate['subject'], 'disease Z', 'Another feature of disease Z', 'context_reference'),
                 "object": alignment(candidate['object'], 'the cooking quality of crop R', span)}}
    result = validate_source_claim(candidate, ReplyClient(reply))
    assert result['decision'] == 'entailed'
    assert result['entity_alignment'] == reply['entity_alignment']
    assert 'alignment_adjustments' not in result  # Never replace a model-selected quotation locally.
    for incorrect in ('Invented quotation', 'A different disease'):
        bad = copy.deepcopy(reply)
        bad['entity_alignment']['subject']['evidence'] = incorrect
        assert validate_source_claim(candidate, ReplyClient(bad))['decision'] == 'needs_review'


def test_added_health_inference_in_claim_description_cannot_pass_source_check():
    candidate = {**source_claim(), 'claim_text': 'Organic farming reduces pollution and prevents human cancer.',
                 'claim_text_origin': 'extracted'}
    reply = {**source_reply(), 'claim_text_match': False}
    result = validate_source_claim(candidate, ReplyClient(reply))
    assert result['decision'] == 'needs_review'
    assert 'Contextual claim text' in result['reasons'][0]
    reply['claim_text_match'] = True
    assert validate_source_claim(candidate, ReplyClient(reply))['decision'] == 'entailed'


def test_measured_traits_can_form_direct_edges_without_adding_entity_types():
    assert schema_error({'subject_type': 'PlantTrait', 'object_type': 'PlantTrait', 'relation': 'reduces'}) is None
    assert 'processing' in SCOPE_POLICY and 'aggregate harvest tonnage' in SCOPE_POLICY


def test_typography_detects_a_new_topic_without_disease_specific_names():
    class Page:
        def extract_text(self, visitor_text):
            visitor_text('Food processing in region S\n', None, None, {'/BaseFont': '/Subset+Font.B'}, 1)
            visitor_text('A plain body line', None, None, {'/BaseFont': '/Regular'}, 1)
    headings = layout_heading_candidates(Page(), 'Food processing in region S\nA plain body line')
    assert headings == {'Food processing in region S'}


def test_discard_record_retains_the_exact_bad_anchors_and_description():
    item = {'subject': 'disease Z of crop R', 'relation': 'reduces', 'object': 'crop light interception',
            'subject_type': 'PlantDisease', 'object_type': 'PlantProcess', 'certainty': 'descriptive',
            'subject_source_mention': 'Disease Z reduces', 'object_source_mention': 'crop light interception',
            'relation_trigger': 'reduces', 'evidence_sentence_numbers': [1], 'qualifiers': {},
            'claim_text': 'Disease Z reduces crop light interception.'}
    packet = {'section_heading': 'Topic', 'packet_number': 1, 'page_start': 1, 'page_end': 1,
              'word_count': 20, 'estimated_token_count': 28,
              'sentences': [{'text': 'Disease Z reduces crop light interception.', 'page': 1, 'section_heading': 'Topic'}]}
    with TemporaryDirectory() as temporary:
        pdf = Path(temporary) / 'source.pdf'
        pdf.write_bytes(b'fixture PDF parsing is mocked')
        with patch('backend.stages.claim_extraction.extractor.source_paper_title', return_value='Source'), \
                patch('backend.stages.claim_extraction.extractor.pdf_bibliography', return_value={}):
            claims, counts, discards = extract_claims(pdf, ReplyClient({'claims': [item]}), 0, lambda *args: None,
                                                       packets=[packet], source_identifiers={'source_doi': 'fixture'})
    assert len(claims) == 1 and not counts and not discards
    assert 'review_entity_anchor_contains_relation_trigger' in claims[0]['extraction_review_hints']
    assert claims[0]['subject_source_mention'] == 'Disease Z reduces'
    assert claims[0]['claim_text'] == item['claim_text']


def test_anchor_compaction_only_uses_a_literal_canonical_phrase_already_in_the_anchor():
    span = 'Disease Z affects growth through a reduction of light interception.'
    candidate = {'subject': 'Disease Z', 'object': 'light interception'}
    subject, obj, changes = compact_literal_entity_anchors(candidate, 'Disease Z',
                                                         'a reduction of light interception', 'a reduction of', span)
    assert subject == 'Disease Z' and obj == 'light interception'
    assert changes[0]['original_mention'] == 'a reduction of light interception'
    other = {**candidate, 'object': 'photosynthesis'}
    assert compact_literal_entity_anchors(other, subject, 'a reduction of light interception',
                                          'a reduction of', span)[1] == 'a reduction of light interception'


def test_reported_reasoning_and_final_tps_are_distinct_not_invented_decode_rates():
    received = []
    output = {'content': '{}', 'finish_reason': 'stop', 'usage': {
        'prompt_tokens': 100, 'completion_tokens': 400, 'completion_tokens_details': {'reasoning_tokens': 390}}}
    client = OpenCodeClient(Settings(), on_request=received.append)
    with patch.object(client._inference, 'chat', return_value=output), \
            patch('backend.providers.llm.time.monotonic', side_effect=[10., 20.]):
        client.chat_json('system', 'user')
    metrics = received[0]
    assert metrics['effective_output_tps'] == 1
    assert metrics['effective_generated_tps'] == 40
    assert metrics['thinking_observed'] is True
    assert metrics['requested_reasoning_effort'] == 'provider_default'
    assert client.request_metrics['effective_generated_tps'] == 40
    assert 'not engine decode TPS' in format_request_metrics(metrics)
    assert model_reasoning_policy(client.model)['requested_reasoning_effort'] == 'provider_default'
    from backend.providers.inference import normalize_usage
    assert normalize_usage({'completion_tokens': 2})['reasoning_usage_reported'] is False


def test_claims_api_exposes_per_call_diagnostics_and_extraction_discards():
    from backend.api.app import pdf_claims
    from backend.shared.storage import write_jsonl
    with TemporaryDirectory() as temporary:
        output = Path(temporary)
        write_jsonl(output / 'candidates.jsonl', [{'candidate_id': 'one'}])
        write_jsonl(output / 'extraction_rejections.jsonl', [{'reason': 'missing_source_anchor'}])
        write_jsonl(output / 'llm_requests.jsonl', [{'task': 'extraction', 'effective_output_tps': 1.0}])
        with patch('backend.api.app._pdf_path', return_value=Path('paper.pdf')), \
                patch('backend.api.app._output_dir', return_value=output):
            result = pdf_claims('pdf-id')
    assert result['total'] == 1 and result['claims'][0]['validation'] is None
    assert result['model_requests'][0]['task'] == 'extraction'
    assert result['extraction_rejections'][0]['reason'] == 'missing_source_anchor'
