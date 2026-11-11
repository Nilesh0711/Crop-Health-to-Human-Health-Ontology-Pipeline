"""Experimental row decoding preserves values and rejects malformed positions."""
from copy import deepcopy
import json
from types import SimpleNamespace

import pytest

from backend.providers.structured_output import SchemaValidationError
from backend.stages.claim_extraction import row_format
from backend.stages.claim_extraction.prompt import build_extraction_prompt
from backend.stages.evaluation.extraction_format import run_comparison
from backend.shared.artifacts import save_rows
from backend.stages.claim_extraction.contract import EXTRACTION_RESPONSE_SCHEMA
from tests.backend.test_extraction_targets import packet, row


def sample():
    return ['Treatment T', 'may reduce', 'root growth', [1, 3], 'Crop C', '',
            '20 mg/L; seven days, "low nutrient" treatment', 'untreated plants', '15% lower']


def test_decode_keeps_qualifier_details_and_sparse_sentence_ids_without_mutation():
    source = {'claims': [sample()]}
    before = deepcopy(source)
    output = row_format.decode(json.loads(json.dumps(source)), [1, 2, 3])
    claim = output['claims'][0]
    assert claim['subject'] == 'Treatment T' and claim['relation'] == 'may reduce'
    assert claim['evidence_sentence_ids'] == [1, 3]
    assert claim['qualifiers']['condition'] == sample()[6]
    assert claim['qualifiers']['metric'] == '15% lower'
    assert 'population' not in claim['qualifiers']
    assert source == before
    assert row_format.decode({'claims': []}, [1]) == {'claims': []}


@pytest.mark.parametrize('bad', [sample()[:-1], sample() + ['extra'],
    sample()[:3] + ['1,3'] + sample()[4:], sample()[:6] + [None] + sample()[7:],
    sample()[:3] + [[1, 99]] + sample()[4:], sample()[:3] + [[True]] + sample()[4:]])
def test_bad_rows_are_rejected_without_position_inference(bad):
    with pytest.raises(SchemaValidationError):
        row_format.decode({'claims': [bad]}, [1, 2, 3])


def test_row_prompt_preserves_runtime_scope_but_replaces_object_instructions():
    original = build_extraction_prompt(0, 'target_context') + '\nACTIVE APPLICATION SCOPE: approved crop domain'
    prompt = row_format.prompt(original)
    assert '# OUTPUT: FIVE REQUIRED FIELDS' not in prompt
    assert 'exactly nine values' in prompt
    assert 'TARGET/CONTEXT CONTRACT' in prompt
    assert 'ACTIVE APPLICATION SCOPE: approved crop domain' in prompt
    assert 'One independently stated relationship per record' in prompt


def test_comparison_uses_same_source_and_settings_and_never_rewrites_inputs(tmp_path):
    target = packet([row(1, text='Treatment T may reduce root growth.')])
    target.update(target_sentence_numbers=[1], target_word_count=8)
    system = build_extraction_prompt(0, 'target_context')
    case = {'number': 1, 'target': target, 'schema': EXTRACTION_RESPONSE_SCHEMA,
            'system': system, 'user': {'local': 'Frozen identical source'}}
    save_rows(tmp_path / 'inputs.jsonl', [case])
    before = (tmp_path / 'inputs.jsonl').read_bytes()
    calls = []
    class Client:
        model = 'test'
        settings = SimpleNamespace(json_regeneration_attempts=0, json_repair_attempts=0,
            task_thinking={'extraction': False}, extraction_min_output_budget=50000)
        def chat_json(self, system, user, **kwargs):
            calls.append((user, kwargs['json_schema']))
            value = sample(); value[3] = [1]
            result = {'claims': [value]} if 'FIXED-POSITION JSON ROWS' in system else row_format.decode({'claims': [value]}, [1])
            kwargs['response_check'](result)
            return result
    report = run_comparison(tmp_path, lambda *args: Client())
    assert len(calls) == 2 and calls[0][0] == calls[1][0]
    assert report['summary']['rows']['valid_first_pass'] == 1
    assert report['differences'][0]['identical_records'] == 1
    assert not report['live_results_modified'] and not report['scientific_accuracy_evaluated']
    assert (tmp_path / 'inputs.jsonl').read_bytes() == before
