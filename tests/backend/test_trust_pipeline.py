"""The new document flow preserves source gates and builds provisional identities first."""
from copy import deepcopy

from backend.shared.storage import read_jsonl
from backend.shared.artifacts import save_rows
from backend.stages.pathway_assembly.corpus import collect_corpus, corpus_signature, ASSEMBLY_VERSION
from backend.stages.pathway_assembly.assembly import read_assembly
from backend.stages.source_validation.review import apply_manual_source_review, source_review_state
from tests.backend.test_pipeline_resume import run_fixture
from tests.backend.test_source_abbreviations import fixture, Client as SourceClient


def test_document_has_no_trust_mode_or_per_claim_external_flow(run_fixture):
    case=run_fixture
    summary=case.run()
    assert summary['knowledge_building_mode']=='pathway_only'
    assert summary['source_entailed']==2
    assert summary['pathway_validation']['individual_external_calls']==0
    assert not (case.output / 'trust_scores.jsonl').exists()
    case.run(resume=True)
    assert case.calls['extraction']==1 and case.calls['external']==[]


def test_manual_source_rejection_retains_the_original_assessment():
    claim, response = fixture()
    original = {'candidate_id': claim['candidate_id'], 'decision': 'unsupported', 'rationale': 'Ambiguous direction.'}
    claim['source_validation'] = deepcopy(original)
    response.update(decision='unsupported', relation_match=False, direction_match=False, certainty_match=False)
    event = apply_manual_source_review(claim, 'source_fidelity', 'reject', expected_revision=source_review_state(claim)['revision'])
    assert source_review_state(claim)['fidelity_decision'] == 'unsupported'
    assert event['model_assessment'] == original
    assert {k: v for k, v in claim['source_validation'].items() if k != 'manual_reviews'} == original


def test_corrected_claim_invalidates_paper_cache_and_replaces_score_without_adding_votes(tmp_path):
    from tests.external.test_trust_graph import fixtures, GroupClient, config
    from archive.legacy_pipeline.research_historical.trust_graph.workflow import assess_group
    claims, packets, answers = fixtures(1, 1)
    client = GroupClient(answers)
    first, _, _ = assess_group(claims, packets, client, config(), tmp_path)
    claims[0]['repair_revision'] = 1
    second, _, _ = assess_group(claims, packets, client, config(), tmp_path)
    assert len(client.state['calls']) == 4
    assert first[0]['trust']['score'] == second[0]['trust']['score'] == 1
