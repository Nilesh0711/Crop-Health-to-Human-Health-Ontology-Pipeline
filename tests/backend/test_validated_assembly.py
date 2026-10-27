"""Admission and stale-snapshot safety for validated-only pathway assembly."""
from copy import deepcopy

import pytest

from backend.config.settings import Settings
from backend.shared.artifacts import save_rows
from backend.stages.pathway_assembly.assembly import assemble_pathways, read_assembly
from backend.stages.pathway_assembly.corpus import collect_corpus
from tests.backend.test_pathway_assembly import saved_claim, Aligner, configuration


@pytest.mark.parametrize('problem', ['source_review', 'source_unsupported', 'no_source', 'no_external',
    'not_found', 'paper_review', 'contradiction', 'contested', 'source_reference_only', 'out_of_scope',
    'error', 'schema', 'semantic_review', 'external_in_progress', 'unassessed_scope', 'stale_support_count'])
def test_nonvalidated_claims_never_enter_assembly(problem, tmp_path):
    from backend.shared.storage import read_jsonl
    folder, claim = saved_claim(tmp_path, 'paper', 'wheat', 'has_contaminant', 'mycotoxins')
    result = read_jsonl(folder / 'validation_runs.jsonl')[0]
    if problem in {'source_review', 'source_unsupported'}:
        claim['source_validation']['decision'] = 'needs_review' if problem == 'source_review' else 'unsupported'
    elif problem == 'no_source':
        claim.pop('source_validation')
    elif problem == 'schema':
        claim['schema_error'] = 'Invalid structure'
    elif problem == 'semantic_review':
        claim['semantic_validation']['decision'] = 'needs_review'
    elif problem in {'no_external', 'not_found'}:
        result = {'candidate_id': claim['candidate_id'], 'validation_status': 'external_evidence_not_found', 'attempts': []}
    elif problem == 'paper_review':
        for d in result['attempts'][0]['decisions']: d['decision'] = 'needs_review'
        result['support_summary']['independent_groups'] = 0
    elif problem in {'contradiction', 'contested'}:
        for d in result['attempts'][0]['decisions']: d['decision'] = 'contradicted'
        if problem == 'contested': result['attempts'][0]['decisions'][0]['decision'] = 'supported'
    elif problem == 'source_reference_only':
        for d in result['attempts'][0]['decisions']: d['evidence_level'] = 'source_reference'
        result['support_summary']['independent_groups'] = 0
    elif problem == 'out_of_scope':
        result['attempts'][0]['joint_assessment'] = {'semantic_assessment': {'in_scope': False}}
    elif problem == 'error':
        result['validation_status'] = 'pipeline_error'
    elif problem == 'external_in_progress':
        result['validation_status'] = 'external_in_progress'
    elif problem == 'unassessed_scope':
        result['attempts'][0].pop('joint_assessment')
    elif problem == 'stale_support_count':
        result['attempts'][0]['decisions'] = []
    save_rows(folder / 'candidates.jsonl', [claim])
    save_rows(folder / 'validation_runs.jsonl', [] if problem == 'no_external' else [result])
    assert collect_corpus(tmp_path) == ([], [])


def test_single_independent_supported_paper_is_eligible(tmp_path):
    folder, claim = saved_claim(tmp_path, 'paper', 'wheat', 'has_contaminant', 'mycotoxins')
    save_rows(folder / 'validation_runs.jsonl', [{'candidate_id': claim['candidate_id'],
        'validation_status': 'world_independent_supported_pending_corroboration', 'support_summary': {'independent_groups': 1},
        'attempts': [{'joint_assessment': {'semantic_assessment': {'in_scope': True}},
                      'decisions': [{'evidence_document_id': 'p', 'decision': 'supported',
                                    'evidence_level': 'independent_external'}]}]}])
    assert collect_corpus(tmp_path)[0][0]['status'] == 'single_source'


def test_downgraded_claim_disappears_from_saved_snapshot_reads_without_model_calls(tmp_path):
    folder, claim = saved_claim(tmp_path, 'paper', 'wheat', 'has_contaminant', 'mycotoxins')
    client = Aligner()
    assemble_pathways(tmp_path, client, configuration())
    calls = deepcopy(client.calls)
    save_rows(folder / 'validation_runs.jsonl', [{'candidate_id': claim['candidate_id'],
        'validation_status': 'external_evidence_not_found', 'attempts': []}])
    data = read_assembly(tmp_path, Settings().pathway_assembly_model)
    assert data['stale']
    assert data['snapshot']['graph']['edges'] == []
    assert data['snapshot']['counts']['claims'] == 0
    assert client.calls == calls
    updated = assemble_pathways(tmp_path, client, configuration())
    assert updated['counts']['claims'] == 0
    assert client.calls == calls


def test_changed_claim_cannot_reuse_old_identity_mapping_in_snapshot_view(tmp_path):
    folder, claim = saved_claim(tmp_path, 'paper', 'wheat', 'has_contaminant', 'mycotoxins')
    assemble_pathways(tmp_path, Aligner(), configuration())
    claim['subject'] = 'rice'
    save_rows(folder / 'candidates.jsonl', [claim])
    data = read_assembly(tmp_path, Settings().pathway_assembly_model)
    assert data['stale'] and data['snapshot']['graph']['edges'] == []
