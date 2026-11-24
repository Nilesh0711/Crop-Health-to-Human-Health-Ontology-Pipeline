"""Frame planning changes instructions, not the JSON shape or semantic guards."""
from backend.stages.claim_extraction.contract import EXTRACTION_RESPONSE_SCHEMA
from backend.stages.claim_extraction.prompt import build_extraction_prompt
from backend.stages.claim_extraction.extractor import check_extraction_response
from backend.config.settings import Settings


def test_schema_and_prompt_put_each_endpoint_next_to_its_context_before_relation():
    schema = EXTRACTION_RESPONSE_SCHEMA['properties']['claims']['items']
    assert 'subject_context' not in schema['properties'] and 'object_context' not in schema['properties']
    assert {'subject_anchor', 'object_anchor'} <= set(schema['properties'])
    assert set(schema['required']) == {'subject', 'object', 'relation', 'qualifiers', 'evidence_sentence_ids'}
    prompt = build_extraction_prompt(0, 'target_context')
    assert 'Both endpoints must differ' in prompt
    assert 'bare markers and headings are not findings' in prompt
    assert 'comparator: baseline or compared group' in prompt
    description = schema['properties']['relation']['description']
    assert 'numbers in qualifiers.metric' in description
    assert 'endpoint_context' not in description
    assert 'qualifiers.comparator' in description and 'qualifiers.population' in description
    assert Settings().task_thinking['extraction'] is True


def test_schema_does_not_require_key_order_or_reject_legitimate_multiword_predicates():
    answer = {'claims': [{'relation': 'may not increase', 'object': 'Trait Q', 'subject': 'Treatment T',
        'evidence_sentence_ids': [1], 'qualifiers': {}}]}
    check_extraction_response(answer, EXTRACTION_RESPONSE_SCHEMA, [1])
