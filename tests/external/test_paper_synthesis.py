"""Independent paper proof, bounded model recovery, synthesis and resume."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.config.settings import Settings
from backend.providers.llm import OpenCodeClient, LLMServiceError, request_log_level
from backend.providers.inference import InferenceHTTP, InferenceError
from backend.providers.response_repair import validate_schema
from backend.providers.structured_output import IncompleteResponseError
from backend.stages.external_validation.paper_proofs import guard_paper, grounded_roles, selected_context, PAPER_DECISION_SCHEMA
from backend.stages.external_validation.synthesis import judge_with_synthesis, _synthesis_documents, _signature, _claim, PREVIOUS_POLICY
from backend.stages.external_validation.workflow import validate_candidate, validation_signature
from backend.stages.graph_construction.graph import graph_from_candidates


def configuration():
    return replace(Settings(), thinking={k: False for k in Settings().task_models})


def example(identifier='paper-a'):
    definition = 'Fusarium head blight (FHB) is a wheat disease.'
    finding = 'FHB reduced grain quality in infected wheat.'
    claim = {'candidate_id': 'claim', 'subject': 'Fusarium head blight', 'object': 'grain quality',
             'relation': 'reduces', 'subject_type': 'PlantDisease', 'object_type': 'PlantTrait',
             'certainty': 'descriptive', 'qualifiers': {'crop_or_species': 'wheat', 'condition': 'Fusarium head blight infection'},
             'semantic_validation': {'decision': 'not_run'}, 'source_validation': {'decision': 'entailed'}}
    doc = {'evidence_document_id': identifier, 'title': 'Synthetic wheat experiment',
           'evidence_level': 'independent_external', 'independence_group': identifier,
           'evidence_chunks': [{'chunk_id': identifier + ':0', 'text': definition + ' ' + finding}]}
    def mapping(canonical, mention, evidence, kind='same_concept'):
        return {'canonical': canonical, 'mention': mention, 'evidence': evidence, 'kind': kind,
                'explanation': 'Identity and conditions are established in this paper.'}
    row = {'decision': 'supported', 'matches': {k: True for k in ('subject','object','relation','direction','certainty')},
           'entity_alignment': {'subject': mapping(claim['subject'], 'FHB', definition, 'explicit_definition'),
                                'object': mapping(claim['object'], 'grain quality', 'grain quality')},
           'qualifier_alignment': {'crop_or_species': mapping('wheat', 'wheat', definition),
                                   'condition': mapping('Fusarium head blight infection', 'infected wheat', 'infected wheat', 'context_reference')},
           'rationale': 'The specified disease reduces this measured outcome in infected wheat.',
           'context_selections': [{'chunk_id': identifier + ':0', 'sentence_numbers': [1]}],
           'evidence': [{'chunk_id': identifier + ':0', 'sentence_numbers': [2], 'subject_mention': 'FHB',
                         'object_mention': 'grain quality', 'relation_trigger': 'reduced', 'stance': 'supports'}]}
    answer = {'decision': row, 'finding': {'summary': 'Reduced grain quality in the tested infected wheat.',
        'study_design': 'Synthetic experiment', 'population_and_conditions': 'Infected wheat',
        'measured_outcome': 'Grain quality', 'limitations': 'Limited to the described experiment.', 'evidence_origin': 'original_result'}}
    return claim, doc, answer


def synthesis(documents, ambiguous=False):
    identifier = documents[0]['evidence_document_id']
    return {'synthesis': 'The compatible paper findings support the specified wheat result.',
        'semantic_assessment': {'subject_type': 'PlantDisease', 'object_type': 'PlantTrait',
            'ambiguous': ambiguous, 'in_scope': True, 'rationale': 'Disease to food-quality mechanism.',
            'evidence_document_ids': [identifier], 'role_evidence': [
                {'role': 'subject', 'evidence_document_id': identifier, 'chunk_id': identifier + ':0', 'sentence_numbers': [1],
                 'entity_mention': 'Fusarium head blight', 'role_trigger': 'wheat disease', 'rationale': 'The text defines the disease.'},
                {'role': 'object', 'evidence_document_id': identifier, 'chunk_id': identifier + ':0', 'sentence_numbers': [2],
                 'entity_mention': 'grain quality', 'role_trigger': 'reduced grain quality', 'rationale': 'A measured food characteristic.'}]},
        'paper_dispositions': [{'evidence_document_id': d['evidence_document_id'], 'disposition': 'retain',
                                'reason': 'Finding is compatible with this fixed claim.'} for d in documents]}


class Client:
    for_model = OpenCodeClient.for_model

    def __init__(self, papers, final, fail_paper=None):
        self.settings = configuration()
        self.model, self.calls, self.papers, self.final = Settings().external_model, [], papers, final
        self.fail_paper, self.last_request_metrics = fail_paper, {'model': self.model, 'status': 'ok'}
    def set_request_context(self, context):
        self.context = context
    def chat_json(self, system, user, **options):
        self.last_request_metrics = {'model': options.get('model', self.model), 'status': 'ok'}
        data = json.loads(user)
        self.calls.append((options['request_label'], data))
        if options['request_label'].startswith('paper external:'):
            identifier = data['documents'][0]['evidence_document_id']
            if identifier == self.fail_paper: raise LLMServiceError('Synthetic provider interruption')
            answer = deepcopy(self.papers[identifier])
        else:
            answer = deepcopy(self.final)
        validate_schema(answer, options['json_schema'])
        options['response_check'](answer)
        return answer


def test_separate_calls_preserve_full_context_ids_and_thinking_off():
    claim, first, one = example()
    _, second, two = example('paper-b')
    docs = [first, second]
    client = Client({'paper-a': one, 'paper-b': two}, synthesis(docs))
    decisions, assessment = judge_with_synthesis(client, claim, docs, configuration())
    assert len(client.calls) == 3
    assert all(len(payload['documents']) == 1 for label, payload in client.calls[:2])
    assert [d['decision'] for d in decisions] == ['supported', 'supported']
    assert all(d['judge']['thinking'] is False for d in decisions)
    assert assessment['thinking'] is False and not assessment['semantic_review_required']
    assert assessment['provider_requests'] == 3
    final = client.calls[-1][1]
    assert len(final['paper_findings']) == len(final['documents']) == 2
    assert final['documents'][0]['chunks'][0]['sentences'][0]['number'] == 1
    assert 'FHB reduced grain quality' in decisions[0]['evidence'][0]['quoted_span']


def test_http_requests_use_deepseek_json_for_papers_and_mimo_schema_for_synthesis(monkeypatch):
    claim, first, one = example(); _, second, two = example('paper-b')
    documents = [first, second]
    answers = iter([one, two, synthesis(documents)])
    payloads, metrics = [], []
    def receive(self, method, url, **kwargs):
        payloads.append(kwargs['json'])
        return {'choices': [{'finish_reason': 'stop', 'message': {'content': json.dumps(next(answers))}}],
                'usage': {'prompt_tokens': 100, 'completion_tokens': 20, 'completion_tokens_details': {'reasoning_tokens': 0}}}
    monkeypatch.setattr(InferenceHTTP, '_request', receive)
    client = OpenCodeClient(configuration(), task='external', on_request=metrics.append)
    decisions, assessment = judge_with_synthesis(client, claim, documents, configuration())
    assert [p['model'] for p in payloads] == ['deepseek-v4.1-flash', 'deepseek-v4.1-flash', 'mimo-v2.6-pro']
    assert [p['response_format']['type'] for p in payloads] == ['json_object', 'json_object', 'json_schema']
    assert all(p['thinking'] == {'type': 'disabled'} for p in payloads)
    assert [m['model'] for m in metrics] == [configuration().external_paper_model] * 2 + [configuration().external_model]
    assert [m['request_id'] for m in metrics] == [1, 2, 3]
    assert all(m['primary_model'] == m['request_model'] == m['model'] and m['next_model'] is None for m in metrics)
    assert all(d['judge']['actual_model'] == configuration().external_paper_model for d in decisions)
    assert client.request_metrics['requests'] == assessment['provider_requests'] == 3
    assert client.request_metrics['input_tokens'] == 300
    assert assessment['phase_models'] == configuration().external_phase_models


def test_paper_proof_recovery_stays_on_deepseek_before_mimo_synthesis():
    claim, doc, answer = example(); invalid = deepcopy(answer)
    invalid['decision']['evidence'][0]['sentence_numbers'] = [999]
    client = OpenCodeClient(configuration(), task='external')
    replies = [{'content': json.dumps(a), 'finish_reason': 'stop'} for a in (invalid, answer, synthesis([doc]))]
    with patch.object(client._inference, 'chat', side_effect=replies) as call:
        decisions, _ = judge_with_synthesis(client, claim, [doc], configuration())
    assert [r.args[0] for r in call.call_args_list] == [configuration().external_paper_model] * 2 + [configuration().external_model]
    assert decisions[0]['decision'] == 'supported'


def test_deepseek_provider_failure_is_reported_as_deepseek_without_pro_substitution():
    claim, doc, _ = example()
    client = OpenCodeClient(configuration(), task='external')
    with patch.object(client._inference, 'chat', side_effect=InferenceError('Synthetic provider failure')) as call:
        with pytest.raises(LLMServiceError, match='external model opencode-go/deepseek-v4.1-flash failed'):
            judge_with_synthesis(client, claim, [doc], configuration())
    assert call.call_count == 1 and call.call_args.args[0] == configuration().external_paper_model
    assert client.last_request_metrics['primary_model'] == configuration().external_paper_model


def test_changing_paper_model_reuses_retrieval_but_rejudges_old_paper_findings():
    from backend.stages.external_validation.workflow import prepare_evidence
    claim, doc, answer = example(); current = configuration()
    old = replace(current, external_paper_model=current.external_model)
    checkpoints = []
    with patch('backend.stages.external_validation.workflow.retrieve_round', return_value={'evidence_documents': [doc]}):
        previous = validate_candidate(claim, Client({'paper-a': answer}, synthesis([doc])), None, old, on_checkpoint=checkpoints.append)
    assert validation_signature(claim, old) != validation_signature(claim, current)
    client = Client({'paper-a': answer}, synthesis([doc]))
    with patch('backend.stages.external_validation.workflow.retrieve_round') as retrieve:
        result = validate_candidate(claim, client, None, current, resume_record=previous)
    retrieve.assert_not_called()
    assert len(client.calls) == 2
    assert result['attempts'][0]['joint_assessment']['reused_paper_judgements'] == 0


def test_semantic_equivalence_is_not_replaced_with_canonical_substring_matching():
    claim, doc, answer = example()
    result = guard_paper(claim, doc, answer['decision'])
    assert result['decision'] == 'supported'
    # The full qualifier does not literally occur: its definition and infection
    # context, rather than a local acronym guess, ground the model's mapping.
    assert 'Fusarium head blight infection' not in doc['evidence_chunks'][0]['text']
    claim['object'] = 'photosynthetic activity'
    doc['evidence_chunks'][0]['text'] = doc['evidence_chunks'][0]['text'].replace('grain quality', 'plant photosynthetic processes')
    row = answer['decision']
    row['evidence'][0]['object_mention'] = 'plant photosynthetic processes'
    row['entity_alignment']['object'].update(canonical='photosynthetic activity', mention='plant photosynthetic processes', evidence='plant photosynthetic processes')
    assert guard_paper(claim, doc, row)['decision'] == 'supported'


def separated_example(numbers):
    claim, doc, answer = example()
    doc['evidence_chunks'][0]['text'] = ' '.join([
        'Fusarium head blight (FHB) is a wheat disease.',
        'The experiment used several wheat plots.',
        'Disease was assessed after inoculation.',
        'FHB infected wheat in the trial.',
        'Untreated controls remained unaffected.',
        'The infected wheat had reduced grain quality.',
        'A separate barley trial was reported elsewhere.',
        'The authors confirmed this reduction in wheat.',
    ])
    answer['decision']['evidence'][0]['sentence_numbers'] = numbers
    return claim, doc, answer


@pytest.mark.parametrize('numbers', [[4, 6], [4, 6, 8], [4, 5, 6]])
def test_exact_nonadjacent_sentence_lists_are_valid_without_expanding_proof(numbers):
    claim, doc, answer = separated_example(numbers)
    validate_schema(answer['decision'], PAPER_DECISION_SCHEMA)
    result = guard_paper(claim, doc, answer['decision'])
    assert result['decision'] == 'supported'
    proof = result['evidence'][0]
    assert proof['sentence_numbers'] == numbers
    assert proof['is_contiguous'] is (numbers == [4, 5, 6])
    if 5 not in numbers:
        assert 'Untreated controls' not in proof['quoted_span']
        assert proof['intervening_context'][0] == {'number': 5, 'text': 'Untreated controls remained unaffected.'}
        assert '[NON-ADJACENT EVIDENCE]' in proof['quoted_span']
    else:
        assert 'Untreated controls' in proof['quoted_span'] and not proof['intervening_context']


def test_synthesis_receives_intervening_text_as_labeled_context_with_original_ids():
    claim, doc, answer = separated_example([4, 6, 8])
    decision = guard_paper(claim, doc, answer['decision'])
    packets, allowed = _synthesis_documents([doc], [decision])
    sentences = {s['number']: s for s in packets[0]['chunks'][0]['sentences']}
    assert set(sentences) == {1, 4, 5, 6, 7, 8}
    assert [sentences[n]['selection_role'] for n in (4, 6, 8)] == ['selected_finding'] * 3
    assert sentences[5]['selection_role'] == sentences[7]['selection_role'] == 'intervening_context'
    assert sentences[1]['selection_role'] == 'selected_context'
    assert allowed['paper-a']['paper-a:0'] == set(sentences)
    assert decision['evidence'][0]['sentence_numbers'] == [4, 6, 8]


@pytest.mark.parametrize('trigger', ['Untreated controls', 'NON-ADJACENT EVIDENCE', 'trial. The infected'])
def test_intervening_text_gap_markers_and_spliced_phrases_cannot_be_finding_anchors(trigger):
    claim, doc, answer = separated_example([4, 6])
    answer['decision']['evidence'][0]['relation_trigger'] = trigger
    result = guard_paper(claim, doc, answer['decision'])
    assert result['decision'] == 'needs_review'
    assert any('relation_trigger must be literal' in r for r in result['guardrail_reasons'])


def test_word_limit_counts_all_selected_fragments_and_not_intervening_context():
    _, doc, _ = separated_example([4, 6])
    doc['evidence_chunks'][0]['text'] = ('Alpha ' * 100) + 'finished. Gap text. ' + ('Omega ' * 100) + 'finished.'
    _, error = selected_context(doc, {'chunk_id': 'paper-a:0', 'sentence_numbers': [1, 3]}, finding=True)
    assert '180 words' in error
    doc['evidence_chunks'][0]['text'] = 'Alpha result. ' + ('Long ' * 200) + 'context. Omega result.'
    selected, error = selected_context(doc, {'chunk_id': 'paper-a:0', 'sentence_numbers': [1, 3]}, finding=True)
    assert not error and len(selected['intervening_context']) == 1
    assert 'Long' not in selected['quoted_span']


@pytest.mark.parametrize('numbers', [[6, 4], [4, 4, 6], [4, 99], [True, 6]])
def test_nonadjacent_selection_still_rejects_invalid_duplicate_or_unordered_ids(numbers):
    claim, doc, answer = separated_example(numbers)
    assert guard_paper(claim, doc, answer['decision'])['decision'] == 'needs_review'


def test_valid_nonadjacent_findings_need_no_provider_regeneration():
    claim, doc, answer = separated_example([4, 6, 8])
    final = synthesis([doc])
    final['semantic_assessment']['role_evidence'][1]['sentence_numbers'] = [6]
    final['semantic_assessment']['role_evidence'][1]['role_trigger'] = 'reduced grain quality'
    client = OpenCodeClient(configuration(), task='external')
    replies = [{'content': json.dumps(a), 'finish_reason': 'stop'} for a in (answer, final)]
    with patch.object(client._inference, 'chat', side_effect=replies) as call:
        decisions, assessment = judge_with_synthesis(client, claim, [doc], configuration())
    assert call.call_count == 2 and decisions[0]['decision'] == 'supported'
    assert not assessment['semantic_review_required']


def test_previous_policy_paper_response_is_rechecked_and_reused_with_exact_selected_ids():
    claim, doc, answer = separated_example([4, 6, 8])
    signature = _signature({'policy': PREVIOUS_POLICY, 'claim': _claim(claim), 'document': doc,
                            'model': configuration().external_paper_model, 'thinking': False})
    saved = [{'evidence_document_id': 'paper-a', 'input_signature': signature,
              'response': answer, 'decision': {'decision': 'needs_review', 'judge': {'thinking': False}}}]
    final = synthesis([doc]); final['semantic_assessment']['role_evidence'][1]['sentence_numbers'] = [6]
    client = Client({}, final)
    decisions, assessment = judge_with_synthesis(client, claim, [doc], configuration(), saved_records=saved)
    assert len(client.calls) == 1 and client.calls[0][0].startswith('cross-paper')
    assert assessment['reused_paper_judgements'] == 1 and decisions[0]['decision'] == 'supported'
    assert decisions[0]['evidence'][0]['sentence_numbers'] == [4, 6, 8]
    assert saved[0]['decision']['decision'] == 'needs_review'  # Historical proposal is unchanged.


def test_nonadjacent_contradiction_is_reverified_without_expanding_selected_proof():
    from backend.stages.external_validation.joint import verified_contradictions
    claim, doc, answer = separated_example([4, 6])
    claim['relation'] = 'increases'
    row = answer['decision']; row['decision'] = 'contradicted'; row['matches']['relation'] = False
    row['evidence'][0]['stance'] = 'contradicts'
    decision = guard_paper(claim, doc, row)
    assert decision['decision'] == 'contradicted'
    validation = {'validation_status': 'world_contradicted_or_contested',
                  'attempts': [{'evidence_documents': [doc], 'decisions': [decision]}]}
    assert verified_contradictions(claim, validation) == [decision]
    decision['evidence'][0]['quoted_span'] += ' Invented text.'
    assert not verified_contradictions(claim, validation)


@pytest.mark.parametrize('problem', ['another_paper', 'invented_quote', 'different_crop', 'unconfirmed_outcome', 'bad_ids', 'false_contradiction'])
def test_invalid_or_semantically_unmatched_proposals_are_never_promoted(problem):
    claim, doc, answer = example()
    row = answer['decision']
    if problem == 'another_paper': row['evidence'][0]['chunk_id'] = 'paper-b:0'
    elif problem == 'invented_quote': row['evidence'][0]['relation_trigger'] = 'greatly improved'
    elif problem == 'different_crop': row['qualifier_alignment']['crop_or_species'].update(mention='oat', kind='different')
    elif problem == 'unconfirmed_outcome': row['matches']['object'] = False
    elif problem == 'bad_ids': row['evidence'][0]['sentence_numbers'] = [999]
    else:
        row['decision'] = 'contradicted'; row['evidence'][0]['stance'] = 'contradicts'
    assert guard_paper(claim, doc, row)['decision'] == 'needs_review'


def test_synthesis_cannot_promote_a_rejected_paper_or_move_a_quote():
    claim, doc, answer = example()
    answer['decision']['matches']['object'] = False
    client = Client({'paper-a': answer}, synthesis([doc]))
    decisions, _ = judge_with_synthesis(client, claim, [doc], configuration())
    assert decisions[0]['decision'] == 'needs_review'
    assert not decisions[0]['evidence']
    final = synthesis([doc])
    final['semantic_assessment']['role_evidence'][0]['evidence_document_id'] = 'invented'
    with pytest.raises(Exception):
        judge_with_synthesis(Client({'paper-a': example()[2]}, final), claim, [doc], configuration())


def test_all_papers_accounted_for_even_when_no_quoted_findings_exist():
    claim, doc, answer = example()
    answer['decision'].update(decision='insufficient', evidence=[], context_selections=[])
    final = synthesis([doc]); final['semantic_assessment'].update(role_evidence=[], ambiguous=True)
    client = Client({'paper-a': answer}, final)
    decisions, _ = judge_with_synthesis(client, claim, [doc], configuration())
    payload = client.calls[-1][1]
    assert len(payload['paper_findings']) == len(payload['documents']) == 1
    assert payload['documents'][0]['chunks'] == []
    assert decisions[0]['decision'] == 'insufficient'


def test_completed_papers_checkpoint_and_resume_without_repeating_calls():
    claim, first, one = example(); _, second, two = example('paper-b')
    docs, checkpoints = [first, second], []
    client = Client({'paper-a': one, 'paper-b': two}, synthesis([first, second]), fail_paper='paper-b')
    with patch('backend.stages.external_validation.workflow.retrieve_round', return_value={'evidence_documents': docs}):
        with pytest.raises(LLMServiceError):
            validate_candidate(claim, client, None, configuration(), on_checkpoint=checkpoints.append)
    saved = checkpoints[-1]
    assert saved['validation_status'] == 'pipeline_error'
    assert len(saved['attempts'][0]['paper_judgements']) == 1
    resumed = Client({'paper-a': one, 'paper-b': two}, synthesis(docs))
    with patch('backend.stages.external_validation.workflow.retrieve_round') as retrieve:
        result = validate_candidate(claim, resumed, None, configuration(), resume_record=saved)
    retrieve.assert_not_called()
    assert len(resumed.calls) == 2 and '/ paper-b' in resumed.calls[0][0]
    assert result['validation_status'] == 'world_independently_corroborated_pending_review'
    assert result['attempts'][0]['joint_assessment']['reused_paper_judgements'] == 1


def test_classification_review_keeps_support_visible_but_excludes_validated_graph():
    claim, doc, answer = example()
    client = Client({'paper-a': answer}, synthesis([doc], ambiguous=True))
    with patch('backend.stages.external_validation.workflow.retrieve_round', return_value={'evidence_documents': [doc]}):
        result = validate_candidate(claim, client, None, configuration())
    assert result['validation_status'] == 'external_support_needs_review'
    assert result['attempts'][0]['decisions'][0]['decision'] == 'supported'
    assert not graph_from_candidates([claim], [result])['edges']
    assert graph_from_candidates([claim], [result], view='audit')['edges']


def test_background_agreement_is_not_counted_as_new_experimental_replication():
    claim, one, a = example(); _, two, b = example('paper-b')
    a['finding']['evidence_origin'] = b['finding']['evidence_origin'] = 'background'
    client = Client({'paper-a': a, 'paper-b': b}, synthesis([one, two]))
    with patch('backend.stages.external_validation.workflow.retrieve_round', return_value={'evidence_documents': [one,two]}):
        result = validate_candidate(claim, client, None, configuration())
    assert result['validation_status'] == 'world_independent_supported_pending_corroboration'
    assert result['support_summary']['independent_groups'] == 0
    assert result['support_summary']['independent_document_groups'] == 2


def test_same_model_bounded_proof_regeneration_respects_thinking_off():
    claim, doc, good = example()
    invalid = deepcopy(good); invalid['decision']['evidence'][0]['sentence_numbers'] = [999]
    final = synthesis([doc])
    responses = [{'content': json.dumps(a), 'finish_reason': 'stop'} for a in (invalid, good, final)]
    client = OpenCodeClient(configuration(), task='external')
    with patch.object(client._inference, 'chat', side_effect=responses) as call:
        decisions, assessment = judge_with_synthesis(client, claim, [doc], configuration())
    assert len(call.call_args_list) == 3 and decisions[0]['decision'] == 'supported'
    assert assessment['thinking'] is False and client.thinking_enabled is False
    assert assessment['provider_requests'] == 3


def test_different_verbatim_endpoint_substrings_do_not_require_identical_spelling():
    claim, doc, answer = example()
    answer['decision']['evidence'][0]['subject_mention'] = 'FHB reduced'
    # Both spans occur in the same finding; the evidence model owns identity.
    assert guard_paper(claim, doc, answer['decision'])['decision'] == 'supported'


def test_exhausted_proof_recovery_saves_review_and_continues_other_papers(tmp_path):
    claim, first, invalid = example(); _, second, good = example('paper-b')
    invalid['decision']['qualifier_alignment']['condition']['mention'] = 'infected ... wheat'
    docs, metrics, progress, checkpoints = [first, second], [], [], []
    client = OpenCodeClient(configuration(), task='external', on_request=metrics.append,
                            on_progress=progress.append, response_dir=tmp_path)
    responses = [{'content': json.dumps(a), 'finish_reason': 'stop'}
                 for a in (invalid, invalid, good, synthesis(docs))]
    with patch.object(client._inference, 'chat', side_effect=responses) as call:
        decisions, assessment = judge_with_synthesis(client, claim, docs, configuration(), on_paper=checkpoints.append)
    assert call.call_count == 4
    assert decisions[0]['decision'] == 'needs_review' and not decisions[0]['evidence']
    assert decisions[0]['rejected_model_decision']['qualifier_alignment']['condition']['mention'] == 'infected ... wheat'
    assert decisions[1]['decision'] == 'supported'
    assert len(checkpoints[-1]) == 2
    assert [m['status'] for m in metrics] == ['evidence_selection_error', 'evidence_needs_review', 'ok', 'ok']
    assert [request_log_level(m) for m in metrics[:2]] == ['warning', 'warning']
    assert metrics[1]['failure_category'] == 'evidence_selection_error'
    assert not metrics[1]['recovery_pending'] and not metrics[1]['recovery_completed']
    assert any('continuing without accepting invalid proof' in str(p.get('provider_status')) for p in progress)
    archive = json.loads(Path(metrics[1]['response_artifact']).read_text(encoding='utf-8'))
    assert archive['accepted_response'] is None and archive['review_response'] == invalid
    assert assessment['thinking'] is False
    assert all(m['requested_thinking'] == 'disabled' for m in metrics)


def test_exhausted_role_selection_recovery_preserves_support_but_requires_review():
    claim, doc, good = example(); final = synthesis([doc])
    final['semantic_assessment']['role_evidence'][0]['sentence_numbers'] = [999]
    metrics = []
    client = OpenCodeClient(configuration(), task='external', on_request=metrics.append)
    replies = [{'content': json.dumps(a), 'finish_reason': 'stop'} for a in (good, final, final)]
    with patch.object(client._inference, 'chat', side_effect=replies) as call:
        decisions, assessment = judge_with_synthesis(client, claim, [doc], configuration())
    assert call.call_count == 3 and decisions[0]['decision'] == 'supported'
    assert assessment['semantic_review_required']
    assert assessment['semantic_assessment']['rejected_role_evidence'][0]['sentence_numbers'] == [999]
    assert all(r['sentence_numbers'] != [999] for r in assessment['semantic_assessment']['role_evidence'])
    assert metrics[-1]['status'] == 'evidence_needs_review'


def test_disabled_proof_recovery_saves_review_without_an_extra_call():
    claim, doc, invalid = example()
    invalid['decision']['evidence'][0]['sentence_numbers'] = [999]
    settings = replace(configuration(), json_regeneration_attempts=0)
    client = OpenCodeClient(settings, task='external')
    replies = [{'content': json.dumps(a), 'finish_reason': 'stop'} for a in (invalid, synthesis([doc]))]
    with patch.object(client._inference, 'chat', side_effect=replies) as call:
        decisions, _ = judge_with_synthesis(client, claim, [doc], settings)
    assert call.call_count == 2 and decisions[0]['decision'] == 'needs_review'


def test_syntax_repair_with_unresolved_proof_cannot_start_a_third_attempt():
    claim, doc, invalid = example()
    invalid['decision']['evidence'][0]['sentence_numbers'] = [999]
    broken = json.dumps(invalid, separators=(',', ':')).replace(',"finding":', ' "finding":')
    patch_answer = {'edits': [{'before': ' "finding":', 'after': ',"finding":'}]}
    metrics = []
    client = OpenCodeClient(configuration(), task='external', on_request=metrics.append)
    final = synthesis([doc]); final['semantic_assessment'].update(role_evidence=[], ambiguous=True)
    replies = [{'content': content, 'finish_reason': 'stop'} for content in (broken, json.dumps(patch_answer), json.dumps(final))]
    with patch.object(client._inference, 'chat', side_effect=replies) as call:
        decisions, _ = judge_with_synthesis(client, claim, [doc], configuration())
    assert call.call_count == 3 and decisions[0]['decision'] == 'needs_review'
    assert [m['purpose'] for m in metrics[:2]] == ['judgement', 'format_repair']
    assert metrics[1]['status'] == 'evidence_needs_review'
    assert metrics[1]['response_format_adjustments'] == [{'kind': 'model_syntax_patch', 'edit_count': 1}]


@pytest.mark.parametrize('second', [
    {'content': '{}', 'finish_reason': 'length'},
    {'content': '{"decision":', 'finish_reason': 'stop'},
    {'content': '{}', 'finish_reason': 'stop'},
])
def test_proof_recovery_never_softens_truncated_malformed_or_schema_invalid_responses(second):
    claim, doc, invalid = example()
    invalid['decision']['evidence'][0]['sentence_numbers'] = [999]
    client = OpenCodeClient(configuration(), task='external')
    with patch.object(client._inference, 'chat', side_effect=[{'content': json.dumps(invalid), 'finish_reason': 'stop'}, second]) as call:
        with pytest.raises(LLMServiceError):
            judge_with_synthesis(client, claim, [doc], configuration())
    assert call.call_count == 2


def test_signature_distinguishes_thinking_and_architecture_without_discarding_retrieval():
    claim, _, _ = example(); off = configuration()
    assert validation_signature(claim, off) != validation_signature(claim, replace(off, thinking={'external': True}))
    assert validation_signature(claim, off) != validation_signature(claim, replace(off, external_judgement_mode='joint'))


def test_failed_synthesis_resumes_all_completed_papers_without_repeating_them():
    claim, doc, answer = example(); checkpoints = []
    class Interrupted(Client):
        def chat_json(self, system, user, **options):
            if options['request_label'].startswith('cross-paper'):
                raise LLMServiceError('Synthesis transport failure')
            return super().chat_json(system, user, **options)
    with patch('backend.stages.external_validation.workflow.retrieve_round', return_value={'evidence_documents': [doc]}):
        with pytest.raises(LLMServiceError):
            validate_candidate(claim, Interrupted({'paper-a': answer}, synthesis([doc])), None, configuration(), on_checkpoint=checkpoints.append)
    resumed = Client({'paper-a': answer}, synthesis([doc]))
    with patch('backend.stages.external_validation.workflow.retrieve_round') as retrieve:
        result = validate_candidate(claim, resumed, None, configuration(), resume_record=checkpoints[-1])
    retrieve.assert_not_called()
    assert len(resumed.calls) == 1 and resumed.calls[0][0].startswith('cross-paper')
    assert result['validation_status'] == 'world_independent_supported_pending_corroboration'


def test_previous_joint_packet_is_reused_when_switching_architecture():
    from backend.stages.external_validation.workflow import prepare_evidence
    claim, doc, _ = example(); off = configuration()
    with patch('backend.stages.external_validation.workflow.retrieve_round', return_value={'evidence_documents': [doc]}):
        old = prepare_evidence(claim, None, None, replace(off, external_judgement_mode='joint'))
    old['attempts'] = [deepcopy(old)]
    with patch('backend.stages.external_validation.workflow.retrieve_round') as retrieve:
        current = prepare_evidence(claim, None, None, off, resume_record=old)
    retrieve.assert_not_called()
    assert current['evidence_documents'] == [doc]


def test_synthesis_context_is_compact_without_renumbering_or_omitting_papers():
    claim, doc, answer = example()
    doc['evidence_chunks'][0]['text'] += ' Unrelated results in another experiment.' * 100
    decision = guard_paper(claim, doc, answer['decision'])
    packets, allowed = _synthesis_documents([doc], [decision])
    assert [s['number'] for s in packets[0]['chunks'][0]['sentences']] == [1,2]
    assert len(json.dumps(packets)) < len(json.dumps(doc))
    sem = synthesis([doc])['semantic_assessment']
    sem['role_evidence'][1]['sentence_numbers'] = [3]
    assert grounded_roles(sem, [doc], allowed)[1]


def test_different_study_ranges_can_be_withheld_without_accepting_a_false_conflict():
    claim, doc, answer = example()
    answer['decision']['decision'] = 'contradicted'
    answer['decision']['matches']['direction'] = False
    answer['decision']['evidence'][0]['stance'] = 'contradicts'
    final = synthesis([doc]); final['paper_dispositions'][0].update(
        disposition='needs_review', reason='The studies used different conditions and their numerical ranges are compatible.')
    client = Client({'paper-a': answer}, final)
    decisions, _ = judge_with_synthesis(client, claim, [doc], configuration())
    assert decisions[0]['decision'] == 'needs_review'
    assert decisions[0]['paper_decision'] == 'contradicted'
    assert decisions[0]['evidence'][0]['quoted_span']


def test_real_oat_text_does_not_support_wheat_when_the_judge_reports_a_mismatch():
    claim, doc, answer = example()
    doc['evidence_chunks'][0]['text'] = doc['evidence_chunks'][0]['text'].replace('wheat', 'oat')
    for mapping in answer['decision']['entity_alignment'].values():
        mapping['evidence'] = mapping['evidence'].replace('wheat', 'oat')
    for mapping in answer['decision']['qualifier_alignment'].values():
        mapping['mention'] = mapping['mention'].replace('wheat', 'oat')
        mapping['evidence'] = mapping['evidence'].replace('wheat', 'oat')
    answer['decision']['qualifier_alignment']['crop_or_species']['kind'] = 'different'
    result = guard_paper(claim, doc, answer['decision'])
    assert result['decision'] == 'needs_review'
    assert any('crop_or_species restriction' in reason for reason in result['guardrail_reasons'])


def test_nli_is_diagnostic_and_cannot_overturn_the_evidence_model():
    claim, doc, answer = example()
    nli = SimpleNamespace(settings=SimpleNamespace(nli_model='diagnostic'),
                          score=lambda *args: {'entailment': 0.001, 'contradiction': 0.998, 'neutral': 0.001})
    decisions, _ = judge_with_synthesis(Client({'paper-a': answer}, synthesis([doc])),
                                       claim, [doc], configuration(), nli_validator=nli)
    assert decisions[0]['decision'] == 'supported'
    assert decisions[0]['nli']['scores']['contradiction'] == 0.998


def test_new_paper_contradiction_enables_repair_only_with_unaltered_exact_proof():
    from backend.stages.external_validation.joint import verified_contradictions
    claim, doc, answer = example()
    claim['relation'] = 'increases'
    answer['decision'].update(decision='contradicted', rationale='The finding decreases the same qualified outcome, opposite to the claim.')
    answer['decision']['matches']['relation'] = False
    answer['decision']['evidence'][0]['stance'] = 'contradicts'
    decisions, _ = judge_with_synthesis(Client({'paper-a': answer}, synthesis([doc])), claim, [doc], configuration())
    validation = {'validation_status': 'world_contradicted_or_contested', 'attempts': [{'evidence_documents': [doc], 'decisions': decisions}]}
    assert len(verified_contradictions(claim, validation)) == 1
    decisions[0]['evidence'][0]['quoted_span'] = 'Invented text.'
    assert verified_contradictions(claim, validation) == []
