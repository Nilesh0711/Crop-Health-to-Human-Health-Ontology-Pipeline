from types import SimpleNamespace
import re

import pytest

from backend.ontology.catalog import Catalog, RESOURCE_DIR
from backend.shared.storage import read_jsonl
from backend.stages.evaluation.extraction_context import prepare_comparison, run_comparison
from tests.backend.test_extraction_targets import row, packet


def test_comparison_freezes_identical_targets_and_uses_production_checks(tmp_path):
    rows = [row(i, text=f'Treatment T affects root length in experiment {i}.') for i in range(1, 10)]
    output = tmp_path / 'comparison'
    reference = Catalog(RESOURCE_DIR)
    prepare_comparison([packet(rows)], reference, output, targets=[5], source_title='Fixture', target_words=10)
    cases = read_jsonl(output / 'inputs.jsonl')
    assert cases[0]['target']['target_sentence_numbers'] == [5]
    before = {p.name: p.read_bytes() for p in output.iterdir()}
    calls = []
    class Client:
        model = 'offline-test-model'
        settings = SimpleNamespace(json_regeneration_attempts=0, json_repair_attempts=0,
            task_thinking={'extraction': True}, extraction_min_output_budget=50000)
        def __init__(self, log):
            self.log = log
        def chat_json(self, system, user, **kwargs):
            core = user.split('<extraction_target>')[1].split('</extraction_target>')[0]
            number = int(re.search(r'\[(\d+)\]', core)[1])
            result = {'claims': [{'subject': 'Treatment T', 'relation': 'affects', 'object': 'root length',
                'qualifiers': {}, 'evidence_sentence_ids': [number]}]}
            kwargs['response_check'](result)
            calls.append((core, kwargs['json_schema'], system))
            self.log({'usage_reported': True, 'input_tokens': len(user) // 4, 'output_tokens': 40, 'reasoning_tokens': 100})
            return result
    report = run_comparison(output, lambda log, path: Client(log))
    assert len(calls) == 2 and calls[0] == calls[1]  # Only the reading context differs.
    assert {p.name: p.read_bytes() for p in output.iterdir() if p.is_file() and p.name in before} == before
    assert report['live_results_modified'] is False and report['scientific_accuracy_evaluated'] is False
    assert report['summary']['local']['unanchored_claims'] == 0
    assert report['summary']['full']['valid_first_pass'] == report['summary']['local']['valid_first_pass'] == 1
    assert report['differences'][0]['identical_records'] == 1
    with pytest.raises(ValueError, match='new frozen comparison'):
        run_comparison(output, lambda log, path: Client(log))


def test_comparison_does_not_hide_invalid_or_empty_endpoints(tmp_path):
    output = tmp_path / 'comparison'
    prepare_comparison([packet([row(1)])], Catalog(RESOURCE_DIR), output, targets=[1], source_title='Fixture')
    class InvalidClient:
        model = 'offline-test-model'
        settings = SimpleNamespace(json_regeneration_attempts=0, json_repair_attempts=0,
            task_thinking={'extraction': True}, extraction_min_output_budget=50000)
        def chat_json(self, *args, **kwargs):
            return {'claims': [{'subject': 'Trait T', 'relation': 'changed', 'object': '',
                'qualifiers': {}, 'evidence_sentence_ids': [1]}]}
    report = run_comparison(output, lambda log, path: InvalidClient())
    assert report['summary']['local']['valid_first_pass'] == report['summary']['full']['valid_first_pass'] == 0
    assert (output / 'results/target-1-local.json').is_file()


def test_comparison_rejects_live_output_and_invalid_target_selection(tmp_path):
    from backend.config.paths import ONTOLOGY_DIR
    ref = Catalog(RESOURCE_DIR)
    with pytest.raises(ValueError, match='outside live ontology'):
        prepare_comparison([packet([row(1)])], ref, ONTOLOGY_DIR / 'test', targets=[1], source_title='Fixture')
    with pytest.raises(ValueError, match='distinct existing'):
        prepare_comparison([packet([row(1)])], ref, tmp_path / 'new', targets=[1, 1], source_title='Fixture')
    assert not (tmp_path / 'new').exists()
