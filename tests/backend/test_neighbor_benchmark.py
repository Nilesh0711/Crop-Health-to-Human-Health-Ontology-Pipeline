"""Offline comparisons preserve data and require evidence-bound human labels."""
import json

import pytest

from backend.shared.artifacts import save_json
from backend.shared.storage import read_jsonl, write_jsonl
from backend.stages.evaluation.neighbors import snapshot_neighbors, compare_neighbors, compare_corpus
from backend.stages.neighbor_validation.records import save_record, write_evaluation_sample
from backend.stages.neighbor_validation.judgement import ground_paper
from tests.external.test_neighbor_validation import case, paper_answer, synthesis_answer


def baseline(case, tmp_path):
    record = {'pathway_id': case.target['pathway_id'], 'target': case.target,
              'verdict': 'connection_supported', 'execution_status': 'complete',
              'assessment': synthesis_answer(case),
              'paper_assessments': [ground_paper(case.target, case.document, paper_answer(case))],
              'calls': {'paper_calls': 1, 'synthesis_calls': 1}}
    save_record(case.root, record)
    run = case.root / 'Document 1'
    write_evaluation_sample(case.root, run)
    output = tmp_path / 'before.json'
    snapshot_neighbors(run, output)
    return run, output


def test_snapshot_and_comparison_never_invent_gold_or_modify_originals(case, tmp_path):
    run, output = baseline(case, tmp_path)
    sources = {p: p.read_bytes() for p in case.root.rglob('*') if p.is_file()}
    result = compare_neighbors(output, output)
    assert result['before']['scientific_errors'] is None
    assert result['before']['independently_reviewed'] == 0
    assert result['before']['per_pair_call_totals']['paper_calls'] == 1
    assert result['paired_targets'][0]['scientific_inputs_unchanged']
    assert not result['automatic_gold_transfer']
    assert all(p.read_bytes() == data for p, data in sources.items())
    with pytest.raises(ValueError, match='outside the source ontology'):
        snapshot_neighbors(run, run / 'destructive.json')
    with pytest.raises(ValueError, match='exists'):
        snapshot_neighbors(run, output)


def test_corpus_comparison_preserves_per_document_results_without_inventing_global_accuracy(case,tmp_path):
    _,before=baseline(case,tmp_path)
    manifest=tmp_path/'corpus.json'
    save_json(manifest,{'comparisons':[{'before':before.name,'after':before.name}]})
    result=compare_corpus(manifest)
    assert result['documents_compared']==1
    assert result['comparisons'][0]['after']['scientific_errors'] is None
    assert result['global_accuracy_aggregated'] is False


@pytest.mark.parametrize('manifest',[{}, {'comparisons':[]}, {'comparisons':[{'before':'file'}]}])
def test_corpus_manifest_reports_missing_configuration(tmp_path,manifest):
    path=tmp_path/'manifest.json'; save_json(path,manifest)
    with pytest.raises(ValueError,match='requires'):
        compare_corpus(path)


def test_human_labels_are_scored_only_for_the_reviewed_evidence(case, tmp_path):
    run, before = baseline(case, tmp_path)
    samples = read_jsonl(run / 'neighbor_evaluation_sample.jsonl')
    samples[0]['gold'].update(reviewer='Independent reviewer', pathway_verdict='unresolved')
    write_jsonl(run / 'neighbor_evaluation_sample.jsonl', samples)
    after = tmp_path / 'after.json'
    snapshot_neighbors(run, after)
    result = compare_neighbors(before, after)
    assert result['after']['scientific_errors']['false_acceptances'] == 1
    value = json.loads(after.read_text(encoding='utf-8'))
    value['evaluation_sample'][0]['review_signature'] = 'outdated-evidence'
    save_json(after, value)
    assert compare_neighbors(before, after)['after']['scientific_errors'] is None


def test_execution_holds_are_distinguished_from_scientific_missed_support(case, tmp_path):
    run, before = baseline(case, tmp_path)
    value = json.loads(before.read_text(encoding='utf-8'))
    value['evaluation_sample'][0]['gold'].update(reviewer='Independent reviewer', pathway_verdict='connection_supported')
    value['records'][0].update(verdict='assessment_incomplete', execution_status='pending')
    after = tmp_path / 'after.json'
    save_json(after, value)
    metrics = compare_neighbors(before, after)['after']['scientific_errors']
    assert metrics['supported_gold_held_by_execution'] == 1
    assert metrics['missed_support'] == 0


def test_snapshot_preserves_retired_contracts_without_counting_them_as_new_results(case, tmp_path):
    run, before = baseline(case, tmp_path)
    from copy import deepcopy
    original = json.loads(before.read_text(encoding='utf-8'))['records'][0]
    old = deepcopy(original)
    old['pathway_id'] = 'retired-pair'
    old['target']['assessment_contract'] = 'earlier-contract'
    save_record(case.root, old)
    after = tmp_path / 'after.json'
    result = snapshot_neighbors(run, after)
    assert result['pairs'] == 1 and result['historical_pairs'] == 1
    frozen = json.loads(after.read_text(encoding='utf-8'))
    assert frozen['historical_records'][0]['pathway_id'] == 'retired-pair'
    report = compare_neighbors(before, after)
    assert report['after']['pairs'] == 1
    assert report['after']['historical_pairs_excluded'] == 1
