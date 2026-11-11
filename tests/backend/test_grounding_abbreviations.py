from backend.ontology.catalog import Catalog, RESOURCE_DIR
from backend.domain.abbreviations import (collect_definitions, definitions, relevant_definitions,
    packet_definitions, expand_claim_names, verify_name_expansion)
from backend.stages.ontology_grounding.grounder import request_packet, ground_claims
from tests.backend.test_ontology_grounding import Grounder, claim


def test_literal_definitions_keep_complete_quotes_and_reverse_forms():
    text = ('Phosphorus (P) and dry matter (DM) were recorded. '
            'PUE (phosphorus utilization efficiency) was measured.')
    rows = list(definitions(text))
    assert [(r['abbreviation'], r['expansion']) for r in rows] == [
        ('P', 'Phosphorus'), ('DM', 'dry matter'), ('PUE', 'phosphorus utilization efficiency')]
    assert [r['quote'] for r in rows] == ['Phosphorus (P)', 'dry matter (DM)', 'PUE (phosphorus utilization efficiency)']
    assert all(r['quote'] in text for r in rows)
    assert list(definitions('P at 20 mg/L; no expansion is supplied.')) == []


def test_definitions_cross_batches_with_document_provenance_and_no_rewriting(tmp_path):
    source = claim('definition', source_evidence_span='Dry matter (DM) was recorded.', source_page=2)
    target = claim('target', subject='DM', source_evidence_span='DM increased.')
    fake = Grounder()
    rows = ground_claims([source, target], tmp_path, Catalog(RESOURCE_DIR), fake, batch_size=1)
    hints = fake.calls[1]['claims']['C1']['abbreviation_definitions']
    assert hints[0]['expansion'] == 'Dry matter'
    assert hints[0]['candidate_id'] == 'definition' and hints[0]['page'] == 2
    assert rows[1]['original_claim']['subject'] == 'DM'
    assert fake.calls[1]['claims']['C1']['subject'] == 'Dry matter'
    assert rows[1]['abbreviation_definitions'] == hints


def test_conflicting_definitions_and_documents_stay_separate():
    a = claim('a', source_document_id='paper-a', subject='DM', source_evidence_span='Dry matter (DM) increased.')
    b = claim('b', source_document_id='paper-a', subject='DM', source_evidence_span='Dry mass (DM) was used.')
    c = claim('c', source_document_id='paper-b', subject='DM', source_evidence_span='DM increased.')
    glossary = collect_definitions([a, b, c])
    hints = relevant_definitions(a, glossary)
    assert len(hints) == 2 and all(r['ambiguous'] for r in hints)
    assert relevant_definitions(c, glossary) == []
    packet, _ = request_packet([a, b, c], Catalog(RESOURCE_DIR))
    assert packet['claims']['C3'].get('abbreviation_definitions', []) == []


def test_changed_definition_invalidates_previously_cached_mapping(tmp_path):
    a = claim('a', source_evidence_span='Dry matter (DM) increased.')
    b = claim('b', subject='DM', source_evidence_span='DM increased.')
    reference = Catalog(RESOURCE_DIR)
    fake = Grounder()
    ground_claims([a, b], tmp_path, reference, fake, batch_size=1)
    a['source_evidence_span'] = 'Dry mass (DM) increased.'
    second = Grounder()
    ground_claims([a, b], tmp_path, reference, second, batch_size=1)
    assert len(second.calls) == 2


def test_definition_can_come_from_an_unextracted_sentence_elsewhere_in_pdf():
    packets = [{'packet_number': 2, 'sentences': [{'text': 'Dry matter (DM) was recorded.', 'page': 3}]}]
    glossary = packet_definitions(packets, 'paper-a')
    target = claim('target', subject='DM', source_document_id='paper-a', source_evidence_span='DM increased.')
    target['source_abbreviation_definitions'] = relevant_definitions(target, glossary)
    packet, _ = request_packet([target], Catalog(RESOURCE_DIR))
    hint = packet['claims']['C1']['abbreviation_definitions'][0]
    assert hint['page'] == 3 and hint['sentence_number'] == 1
    assert hint['quote'] == 'Dry matter (DM)' and hint['context_field'] == 'parsed_sentence'


def test_dotted_scientific_names_expand_only_claim_fields_with_literal_proof_retained():
    evidence = 'B.subtilis increases P uptake.'
    c = claim('source', subject='B.subtilis', object='P uptake',
        source_evidence_span=evidence,
        source_local_context='Bacillus subtilis (B. subtilis) and phosphorus (P) were studied.',
        qualifiers={'condition': 'low P', 'population': 'P0.5'})
    updated = expand_claim_names(c)
    assert updated['subject'] == 'Bacillus subtilis'
    assert updated['object'] == 'phosphorus uptake'
    assert updated['qualifiers'] == {'condition': 'low phosphorus', 'population': 'P0.5'}
    assert updated['source_evidence_span'] == evidence
    assert updated['source_local_context'] == c['source_local_context']
    assert updated['subject_source_mention'] == 'B.subtilis'
    assert updated['candidate_id'] == c['candidate_id']
    assert expand_claim_names(updated) == updated
    assert c['subject'] == 'B.subtilis'


def test_reverse_scientific_definition_and_statistical_labels():
    assert list(definitions('B. subtilis (Bacillus subtilis)'))[0]['expansion'] == 'Bacillus subtilis'
    assert list(definitions('P levels (P), a root (A), DM, and (D)')) == []


def test_nested_source_definitions_agree_without_guessing_other_documents():
    a = claim('a', subject='PUE', source_document_id='a',
        source_evidence_span='Phosphorus (P). P use efficiency (PUE). Phosphorus use efficiency (PUE).')
    b = claim('b', subject='PUE', source_document_id='b', source_evidence_span='PUE was measured.')
    glossary = collect_definitions([a, b])
    assert expand_claim_names(a, glossary)['subject'].casefold() == 'phosphorus use efficiency'
    assert expand_claim_names(b, glossary)['subject'] == 'PUE'


def test_conflicting_names_remain_unchanged_and_flagged():
    c = claim('a', subject='DM', source_evidence_span='Dry matter (DM). Dry mass (DM).')
    updated = expand_claim_names(c)
    assert updated['subject'] == 'DM'
    assert updated['unresolved_abbreviations'] == ['DM']


def test_name_edit_guard_does_not_allow_a_scientific_change_or_invented_quote():
    quote = 'Dry matter (DM)'
    assert verify_name_expansion('DM increases', 'Dry matter increases', 'DM', 'Dry matter', quote, quote)
    assert not verify_name_expansion('DM increases', 'Dry matter decreases', 'DM', 'Dry matter', quote, quote)
    assert not verify_name_expansion('DM increases', 'Dry matter increases', 'DM', 'Dry matter', quote, 'DM increased.')


def test_unit_symbols_are_not_expanded_as_statistical_abbreviations():
    c = claim('a', subject='Culture', object='30 °C',
        qualifiers={'condition': '4 ° C; stored at -20 °C; factor C'},
        source_evidence_span='Cultivar (C) was a factor. Incubation was at 30 °C.')
    updated = expand_claim_names(c)
    assert updated['object'] == '30 °C'
    assert updated['qualifiers']['condition'] == '4 ° C; stored at -20 °C; factor Cultivar'
    assert not verify_name_expansion('30 °C', '30 °Cultivar', 'C', 'Cultivar',
                                     'Cultivar (C)', c['source_evidence_span'])


def test_expanding_an_already_spelled_out_name_does_not_repeat_it():
    c = claim('a', subject='Phosphorus (P)', object='phosphorus use efficiency (PUE)',
        source_evidence_span='Phosphorus (P) affects phosphorus use efficiency (PUE).')
    updated = expand_claim_names(c)
    assert updated['subject'] == 'Phosphorus'
    assert updated['object'] == 'phosphorus use efficiency'
    assert updated['source_evidence_span'] == c['source_evidence_span']
    assert expand_claim_names(updated) == updated
