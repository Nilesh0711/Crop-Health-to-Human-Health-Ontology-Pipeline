"""Generic evidence contracts, not scientific gold or paper-specific runtime rules."""
import pytest

from backend.providers.structured_output import EvidenceSelectionError
from backend.providers.structured_output import SchemaValidationError
from backend.providers.response_repair import validate_schema
from backend.stages.neighbor_validation.judgement import paper_schema
from backend.stages.neighbor_validation.judgement import ground_paper, check_synthesis
from tests.external.test_neighbor_validation import case, paper_answer, synthesis_answer


def test_partial_match_remains_valid_but_is_never_silently_converted_from_invalid_stance(case):
    answer = paper_answer(case)
    for key in ('pathway_id', 'evidence_document_id'):
        answer.pop(key, None)  # These identities are request-bound, not model output.
    answer['edge_findings'][0].update(stance='supports', claim_match='partial')
    schema = paper_schema(case.target, case.document)
    validate_schema(answer, schema)
    answer['edge_findings'][0]['stance'] = 'partial'
    with pytest.raises(SchemaValidationError):
        validate_schema(answer, schema)
    assert answer['edge_findings'][0]['stance'] == 'partial'


@pytest.mark.parametrize('applicability', [False, None])
def test_partial_support_still_requires_established_applicability(case, applicability):
    answer = paper_answer(case)
    answer['edge_findings'][0].update(claim_match='partial', conditions_match=applicability)
    paper = ground_paper(case.target, case.document, answer)
    proposed = synthesis_answer(case, verdict='components_supported', evidence_basis='component_evidence_only')
    proposed['edge_assessments'][0]['stance'] = 'partially_supported'
    with pytest.raises(EvidenceSelectionError, match='false/null applicability'):
        check_synthesis(case.target, [paper], proposed)


def test_contextual_correction_proof_does_not_require_supporting_original_claim(case):
    answer = paper_answer(case)
    answer['edge_findings'][0].update(stance='not_addressed', claim_match='context_only')
    paper = ground_paper(case.target, case.document, answer)
    proposed = synthesis_answer(case, verdict='components_supported', evidence_basis='component_evidence_only')
    proposed['edge_assessments'][0]['stance'] = 'unresolved'
    proposed['corrections'] = [{'edge_id': case.target['edges'][0]['id'],
        'claim_text': 'Factor A changes Process B under Condition K.', 'reason': 'A proof-bound wording proposal.',
        'proof_refs': proposed['edge_assessments'][0]['proof_refs']}]
    check_synthesis(case.target, [paper], proposed)
    proposed['edge_assessments'][0]['stance'] = 'supported'
    with pytest.raises(EvidenceSelectionError):
        check_synthesis(case.target, [paper], proposed)


def test_exclusion_cannot_enter_positive_local_support_but_remains_auditable(case):
    paper = ground_paper(case.target, case.document, paper_answer(case))
    proposed = synthesis_answer(case)
    proposed['connection_assessments'][0]['connection_kind'] = 'exclusion'
    with pytest.raises(EvidenceSelectionError, match='Local support requires'):
        check_synthesis(case.target, [paper], proposed)
    proposed.update(verdict='components_supported', evidence_basis='component_evidence_only')
    check_synthesis(case.target, [paper], proposed)


def test_contextual_host_connection_does_not_need_a_mediated_causal_route(case):
    paper = ground_paper(case.target, case.document, paper_answer(case))
    proposed = synthesis_answer(case)
    proposed['connection_assessments'][0]['connection_kind'] = 'contextual'
    check_synthesis(case.target, [paper], proposed)
