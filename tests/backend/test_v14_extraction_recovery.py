"""Bounded provenance recovery, semantic punctuation and local review signals."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from backend.domain.extraction_review import extraction_warnings
from backend.shared.text import source_surface_mention
from backend.stages.claim_extraction.anchoring import endpoint_anchor, anchor_diagnostic


@pytest.mark.parametrize('source,endpoint', [
    ('Compound A and Compound B concentration increased.', 'Compound A concentration'),
    ('Sugar concentration and content increased.', 'Sugar content'),
    ('Sugar and indole-3-acetic acid concentration in roots increased.', 'Sugar concentration in roots'),
    ('Trait Q in young leaves and older roots increased.', 'Trait Q in older roots'),
    ('Both shoot and root dry matter of Group A and Group B increased.', 'shoot dry matter of Group B'),
    ('Both shoot and root dry matter of Group A and Group B increased.', 'root dry matter of Group B'),
    ('Trait Q in shoots by 12% and in roots by 5% increased.', 'Trait Q in roots'),
    ('Compound Q deficiency and toxicity affected Trait R.', 'Compound Q toxicity'),
])
def test_explicit_shared_words_are_recoverable(source, endpoint):
    result = endpoint_anchor(endpoint, source.rstrip('.'), [source])
    assert result and result[1] == 'bounded_coordination'
    assert result[0] in source


@pytest.mark.parametrize('source,endpoint,anchor', [
    ('Trait Q in shoots declined and in roots increased.', 'Trait Q in roots', ''),
    ('Trait Q in shoots increased. Trait R in roots declined.', 'Trait Q in roots', ''),
    ('Trait Q and Trait R changed.', 'invented effect', 'Trait Q'),
    ('Compound A and Compound B concentration changed.', 'Compound A concentration', 'Compound A'),
    ('Compound A and Compound B concentration rose; Compound A and Compound C concentration fell.',
     'Compound A concentration', ''),
    ('Group A and Group B received Treatment T.', 'Group A caused Trait Q', 'Group A'),
])
def test_no_arbitrary_gap_or_unrelated_quote(source, endpoint, anchor):
    assert endpoint_anchor(endpoint, anchor, [source]) is None


@pytest.mark.parametrize('source,endpoint,expected', [
    ('at 5 mg L − 1', '5 mg L−1', True),
    ('at −5 mg/L', '5 mg/L', False),
    ('at −5 mg/L', '-5 mg/L', True),
    ('at 0.5 mg/L', '0 5 mg/L', False),
    ('at 13–25%', '13-25%', True),
    ('at ≥5 mg/L', '>5 mg/L', False),
    ('HPO4- was present.', 'HPO4', False),
    ('HPO4- was present.', 'HPO4-', True),
    ('Na+ was present.', 'Na', False),
    ('The root-to-shoot ratio changed.', 'root-to-shoot ratio', True),
    ('alloca - tion increased.', 'allocation', True),
])
def test_matching_preserves_values_and_chemical_marks(source, endpoint, expected):
    span = source_surface_mention(source, endpoint)
    assert bool(span) is expected
    if span:
        assert span in source


def test_cited_literal_antecedent_is_supported_without_general_coreference():
    target = 'It affected Trait Q.'
    antecedent = 'Compound Q accumulated in the sample.'
    assert endpoint_anchor('Compound Q', 'Compound Q', [target]) is None
    assert endpoint_anchor('Compound Q', 'Compound Q', [antecedent, target])[0] == 'Compound Q'
    assert endpoint_anchor('Compound Q availability', 'its availability', [antecedent, target]) is None
    assert anchor_diagnostic('Compound Q availability', 'its availability', [target]) == 'unproven_antecedent'


def warning_claim(source, subject, relation, obj, **extra):
    return dict(subject=subject, relation=relation, object=obj, qualifiers={},
                source_evidence_span=source, **extra)


def test_inverted_trend_is_held_but_correct_trend_is_not_rewritten():
    source = 'As Factor T increased, Trait Q declined.'
    wrong = warning_claim(source, 'Factor T', 'declined with increasing', 'Trait Q')
    before = deepcopy(wrong)
    assert 'possible_role_inversion' in {r['code'] for r in extraction_warnings(wrong)}
    assert wrong == before
    right = warning_claim(source, 'Trait Q', 'declined as', 'Factor T')
    assert not extraction_warnings(right)
    unrelated = warning_claim('Factor T increased Trait Q. Trait R declined.', 'Factor T', 'increased', 'Trait Q')
    assert not extraction_warnings(unrelated)


def test_population_substitution_is_reviewed_without_rejecting_real_population_comparison():
    c = warning_claim('Group A showed limited allocation to roots, resulting in lower Trait Q.',
                      'Group A', 'resulted in', 'Trait Q')
    c['qualifiers'] = {'population': 'Group A'}
    assert 'possible_mechanism_substitution' in {r['code'] for r in extraction_warnings(c)}
    c.update(relation='had higher', source_evidence_span='Group A had higher Trait Q than Group B.')
    assert not extraction_warnings(c)


def test_joint_treatments_and_defining_ratios_remain_literal():
    source = 'Combined Treatment A and Treatment B affected the root-to-shoot ratio.'
    for endpoint in ['Combined Treatment A and Treatment B', 'root-to-shoot ratio']:
        assert endpoint_anchor(endpoint, endpoint, [source])[1] == 'literal_endpoint'


def test_frozen_development_sample_is_balanced_and_replay_is_offline():
    from backend.cli.extraction_replay import replay
    sample = json.loads((Path(__file__).parents[1] / 'fixtures/atomic_grounding_v14_review.json').read_text(encoding='utf-8'))
    before = deepcopy(sample)
    result = replay(sample)
    assert sample == before
    assert result['summary']['cases'] == 24
    assert sorted(result['summary']['documents'].values()) == [12, 12]
    assert result['summary']['anchor_expectation_failures'] == 0
    assert result['summary']['baseline_rejected_now_supported'] == 3
    assert result['summary']['model_calls'] == 0
    assert all(r['scientific_review_status'] == 'pending' and r['model_comparison'] == 'not_run' for r in result['cases'])
    roles = {r['case_id']: {w['code'] for w in r['warnings']} for r in result['cases']}
    assert 'possible_role_inversion' in roles['Chea_2024_Potato_Phosphorus_Nutrition:saved-73']
    assert 'possible_mechanism_substitution' in roles['Chea_2024_Potato_Phosphorus_Nutrition:saved-208']


def test_real_extraction_rejection_records_the_failing_role(tmp_path, monkeypatch):
    from backend.stages.claim_extraction.extractor import extract_claims
    from tests.backend.test_extraction_targets import packet, row
    class Client:
        def chat_json(self, *args, **kwargs):
            value = {'claims': [{'subject': 'invented mechanism', 'subject_anchor': 'Treatment T',
                'relation': 'increased', 'object': 'Trait Q', 'object_anchor': 'Trait Q',
                'qualifiers': {}, 'evidence_sentence_ids': [1]}]}
            kwargs['response_check'](value)
            return value
    pdf = tmp_path / 'source.pdf'
    pdf.write_bytes(b'fixture')
    monkeypatch.setattr('backend.stages.claim_extraction.extractor.source_paper_title', lambda *a: 'Abstract fixture')
    _, rejected, records = extract_claims(pdf, Client(), 0, lambda *a: None,
        packets=[packet([row(1, text='Treatment T increased Trait Q.')])],
        source_identifiers={'source_doi': 'fixture'}, bibliography=[])
    assert rejected['unanchored_evidence'] == 1
    assert records[0]['anchor_failures'][0]['role'] == 'subject'
    assert records[0]['anchor_failures'][0]['category'] == 'unsupported_endpoint_surface'
