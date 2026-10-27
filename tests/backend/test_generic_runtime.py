"""Runtime instructions stay independent of particular papers and claim wording."""
from copy import deepcopy
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from backend.stages.claim_extraction.extractor import extraction_review_hints
from backend.stages.claim_extraction.prompt import build_extraction_prompt
from backend.stages.external_validation.synthesis import judge_with_synthesis
from backend.stages.pathway_assembly.contract import SYSTEM as ASSEMBLY_SYSTEM
from backend.stages.pdf_parsing.packets import iter_pdf_context_packets, layout_heading_candidates
from backend.stages.source_validation.checker import validate_source_claim
from tests.backend.test_source_abbreviations import Client as SourceClient, fixture
from tests.external.test_paper_synthesis import Client, configuration, example, synthesis


CASE_TEXT = ('Fusarium', 'FHB', 'Brown spot', 'deoxynivalenol', 'annual wheat',
             'has made FHB a', 'mycotoxin contamination affects grain uses',
             'epidemics generate impoverishment', 'need to produce crop calories protein')


def assert_generic(system):
    assert not any(case.casefold() in system.casefold() for case in CASE_TEXT)


def test_source_instructions_are_generic_while_actual_disease_names_and_evidence_remain_input():
    claim, response = fixture()
    client = SourceClient(response)
    result = validate_source_claim(claim, client)
    assert result['decision'] == 'entailed'
    assert_generic(client.system)
    assert 'FHB reduces grain quality.' in json.dumps(client.payload)
    assert claim['subject'] == 'Fusarium head blight'


def test_paper_and_cross_paper_instructions_are_generic_without_changing_real_evidence():
    claim, document, response = example()
    class RecordingClient(Client):
        def chat_json(self, system, user, **options):
            assert_generic(system)
            return super().chat_json(system, user, **options)
    client = RecordingClient({'paper-a': response}, synthesis([document]))
    decisions, assessment = judge_with_synthesis(client, claim, [document], configuration())
    assert len(client.calls) == 2 and decisions[0]['decision'] == 'supported'
    assert 'FHB reduced grain quality' in decisions[0]['evidence'][0]['quoted_span']
    assert not assessment['semantic_review_required']


@pytest.mark.parametrize('mode', ['whole_packet', 'target_context'])
def test_extraction_and_assembly_examples_use_placeholders_without_weakening_domain_policy(mode):
    prompt = build_extraction_prompt(0, mode)
    assert_generic(prompt)
    assert_generic(ASSEMBLY_SYSTEM)
    assert 'Crop C' in prompt and 'Disease D' in prompt and 'Compound T' in prompt
    assert 'Never collapse A -> B -> C into A -> C' in prompt
    assert 'harvest tonnage/productivity alone' in prompt
    assert 'Never guess an undefined expansion' in prompt
    assert 'Disease D -> infects -> Crop C material' in prompt


@pytest.mark.parametrize('phrase', [
    'uses of the grain', 'need to produce crop calories protein',
    'negative environmental impacts of agricultural production', 'food availability',
])
def test_previously_seen_endpoint_phrases_do_not_trigger_special_scope_hints(phrase):
    claim = {'subject': 'Crop C', 'object': phrase, 'relation': 'affects',
             'certainty': 'descriptive', 'qualifiers': {}}
    original = deepcopy(claim)
    hints = extraction_review_hints(claim, f'Crop C affects {phrase}.')
    assert 'review_generic_endpoint_or_scope' not in hints
    assert claim == original  # Scientific scope is for the evidence model.


class Page:
    def __init__(self, text, runs):
        self.text, self.runs = text, runs

    def extract_text(self, visitor_text=None):
        if visitor_text:
            for text, style in self.runs:
                visitor_text(text, None, None, {'/BaseFont': style}, 10)
        return self.text


def test_typographic_heading_can_span_punctuation_font_runs_without_entity_keywords():
    title = "Topic A's mechanisms in Crop C"
    page = Page(title, [('Topic A', '/Bold'), ("'", '/Regular'), ('s mechanisms in Crop C\n', '/Bold')])
    assert layout_heading_candidates(page, title) == {title}
    authors = 'Author A1 & Author B'
    page = Page(authors, [('Author A', '/Bold'), ('1', '/Regular'), (' & Author B\n', '/Bold')])
    assert layout_heading_candidates(page, authors) == set()


def test_prose_colons_and_disease_place_words_cannot_discard_scientific_body_text():
    heading = "Topic A's mechanisms in Crop C"
    finding = 'The measured effect has two parts: Process P changes and Outcome O improves.'
    plain = 'Disease D of Crop C, Region R'
    text = '\n'.join((heading, finding, plain))
    page = Page(text, [('Topic A', '/Bold'), ("'", '/Regular'), ('s mechanisms in Crop C\n', '/Bold'),
                       (finding+'\n'+plain, '/Regular')])
    inspection = {'regions': [{'page': 1, 'chapter_id': 'one', 'chapter_title': 'Document', 'body': text}]}
    with patch('backend.stages.pdf_parsing.packets.PdfReader', return_value=SimpleNamespace(pages=[page])):
        packets = list(iter_pdf_context_packets(Path('fixture.pdf'), inspection=inspection))
    sentences = [s for p in packets for s in p['sentences']]
    assert finding in ' '.join(s['text'] for s in sentences)
    assert all(s['section_heading'] == heading for s in sentences)
