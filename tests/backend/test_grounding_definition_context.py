"""Source-bound definition context without scientific rewriting or new mappings."""
from copy import deepcopy
import json

from backend.domain.abbreviations import packet_definitions, collect_definitions
from backend.ontology.catalog import catalog
from backend.shared.artifacts import save_rows
from backend.shared.storage import stable_hash, read_jsonl
from backend.stages.ontology_grounding.source_context import contexts_for_claim, definition_contexts, MAX_WORDS, POLICY
from backend.stages.ontology_grounding.grounder import ground_claims, request_packet
from backend.stages.ontology_grounding.contract import GROUNDING_INSTRUCTIONS
from backend.stages.ontology_grounding.rows import SYSTEM as TSV_SYSTEM
from backend.pipelines.ontology import current_assertions
from tests.backend.test_ontology_grounding import Grounder


def source():
    texts = ['Trait Q efficiency (TQE) measures conversion of Material M into biomass.',
             'Plants were subjected to Factor Z levels (1, 2 and 3 mg/L) in a solution.',
             'Factor Z levels in solution were adjusted to 1, 2 and 3 mg/L, labelled as Z1, Z2 and Z3.',
             'TQE was affected by Factor Z levels.']
    packet = {'packet_number': 1, 'sentences': [{'text': t, 'page': 2} for t in texts]}
    claim = {'candidate_id': 'one', 'subject': 'TQE', 'relation': 'was affected by', 'object': 'Factor Z levels',
             'qualifiers': {}, 'source_document_id': 'doc', 'source_pdf_sha256': 'a' * 64,
             'source_packet_number': 1, 'source_context_packet': ' '.join(texts),
             'source_evidence_span': texts[-1], 'source_evidence_sentence_numbers': [4],
             'subject_source_mention': 'TQE', 'object_source_mention': 'Factor Z levels'}
    claim['source_abbreviation_definitions'] = packet_definitions([packet], 'doc')
    return packet, claim, collect_definitions([claim])


def test_source_definitions_and_labelled_variable_settings_are_selected_verbatim():
    packet, claim, glossary = source()
    before = deepcopy(claim)
    found = contexts_for_claim(claim, [packet], glossary)
    assert [r['sentence_number'] for r in found['subject']] == [1]
    assert [r['sentence_number'] for r in found['object']] == [3, 2]
    for rows in found.values():
        for row in rows:
            assert row['text'] == packet['sentences'][row['sentence_number'] - 1]['text']
            assert row['text_sha256'] == stable_hash(row['text'], 64)
            assert row['page'] == 2 and row['source_pdf_sha256'] == 'a' * 64
    assert claim == before


def test_unrelated_results_foreign_packets_and_changed_source_are_not_definitions():
    packet, claim, glossary = source()
    assert contexts_for_claim(claim, [{**packet, 'source_document_id': 'other'}], glossary) == {}
    assert contexts_for_claim(claim, [{**packet, 'source_pdf_sha256': 'b' * 64}], glossary) == {}
    altered = {**claim, 'source_context_packet': 'different source', 'source_abbreviation_definitions': []}
    assert contexts_for_claim(altered, [packet], []) == {}
    unrelated = {'packet_number': 1, 'sentences': [{'text': 'A different variable measures Trait R.', 'page': 1}]}
    c = {**claim, 'source_context_packet': unrelated['sentences'][0]['text']}
    assert contexts_for_claim(c, [unrelated], glossary) == {}


def test_other_packet_abbreviation_sentence_requires_individual_provenance():
    packet, claim, glossary = source()
    other = deepcopy(packet); other['packet_number'] = 2
    c = {**claim, 'source_context_packet': 'unavailable', 'source_packet_number': 9}
    assert contexts_for_claim(c, [other], glossary) == {}
    found = contexts_for_claim(c, [packet], glossary)
    assert found['subject'][0]['sentence_number'] == 1
    assert 'object' not in found


def test_named_arm_inside_an_endpoint_retrieves_verified_methods():
    methods = 'Factor Z levels were adjusted to 1 and 3 mg/L, labelled as Z1 and Z3, respectively.'
    finding = 'Raising the Factor Z level to Z3 increased Trait Q.'
    packet = {'packet_number': 2, 'sentences': [{'text': finding, 'page': 4}]}
    setup = {'packet_number': 1, 'sentences': [{'text': methods, 'page': 2, 'source_sentence_id': 'method-1'}]}
    claim = {'candidate_id': 'one', 'subject': 'Raising the Factor Z level to Z3', 'object': 'Trait Q',
             'relation': 'increased', 'source_context_packet': finding, 'source_packet_number': 2,
             'source_evidence_span': finding, 'source_document_id': 'doc', 'source_pdf_sha256': 'a' * 64}
    before = deepcopy(claim)
    # A bare packet identifier cannot establish another packet's provenance.
    assert contexts_for_claim(claim, [setup, packet], []) == {}
    bound_setup = {**setup, 'source_document_id': 'doc', 'source_pdf_sha256': 'a' * 64}
    found = contexts_for_claim(claim, [bound_setup, packet], [])
    assert list(found) == ['subject']
    assert found['subject'] == [{'label': 'Z3', 'document': 'doc', 'source_pdf_sha256': 'a' * 64,
        'packet_number': 1, 'sentence_number': 1, 'page': 2, 'text': methods, 'text_sha256': stable_hash(methods, 64),
        'policy': 'source-defined-treatment-labels-v1', 'source_sentence_id': 'method-1', 'selection_basis': 'treatment_label'}]
    payload, _ = request_packet([claim], catalog(), source_definitions={'one': found})
    lookup = payload['claims']['C1']['subject_definition_context_ids'][0]
    assert payload['definition_contexts'][lookup]['text'] == methods
    assert payload['claims']['C1']['source_evidence_span'] == finding and claim == before
    assert contexts_for_claim(claim, [{**bound_setup, 'source_pdf_sha256': 'b' * 64}, packet], []) == {}

    conflicting = {**bound_setup, 'packet_number': 3, 'sentences': [{'text': methods.replace('and 3', 'and 8'), 'page': 3}]}
    assert contexts_for_claim(claim, [packet, bound_setup, conflicting], []) == {}
    repeated = {**bound_setup, 'packet_number': 3}
    assert len(contexts_for_claim(claim, [packet, bound_setup, repeated], [])['subject']) == 1
    two_arms = {**claim, 'subject': 'The difference between Z1 and Z3'}
    assert contexts_for_claim(two_arms, [packet, bound_setup], []) == {}


def test_named_arm_uses_verbatim_setup_in_owning_packet():
    packet, claim, _ = source()
    claim.update(subject='Applying Factor Z treatment Z2', subject_source_mention='Factor Z treatment Z2',
                 object='Trait Q', source_abbreviation_definitions=[])
    found = contexts_for_claim(claim, [packet], [])
    assert found['subject'][0]['text'] == packet['sentences'][2]['text']
    assert found['subject'][0]['selection_basis'] == 'treatment_label'


def test_context_is_bounded_without_cutting_sentences_numbers_or_charges():
    packet, claim, glossary = source()
    packet['sentences'].insert(0, {'text': 'Factor Z levels were adjusted to 3 mg/L as H2QO4−; ' + 'word ' * 270, 'page': 2})
    claim['source_context_packet'] = ' '.join(s['text'] for s in packet['sentences'])
    found = contexts_for_claim(claim, [packet], glossary)
    rows = {r['text_sha256']: r for records in found.values() for r in records}
    assert sum(len(r['text'].split()) for r in rows.values()) <= MAX_WORDS
    assert all(r['text'] in claim['source_context_packet'] for r in rows.values())
    assert not any(len(r['text'].split()) > MAX_WORDS for r in rows.values())


def test_inputs_deduplicate_source_context_and_keep_response_formats_unchanged():
    packet, claim, glossary = source()
    claims = [claim, {**deepcopy(claim), 'candidate_id': 'two'}]
    contexts = definition_contexts(claims, [packet], glossary)
    data, _ = request_packet(claims, catalog(), glossary=glossary, source_definitions=contexts)
    assert len(data['definition_contexts']) == 3
    assert data['claims']['C1']['object_definition_context_ids'] == data['claims']['C2']['object_definition_context_ids']
    assert len(data['terms']) == 212 and len(data['relations']) == 25
    assert TSV_SYSTEM.startswith(GROUNDING_INSTRUCTIONS)
    assert 'ENCOMPASSING CLASS' in GROUNDING_INSTRUCTIONS
    assert 'trait describing a process is not the process' in GROUNDING_INSTRUCTIONS
    assert 'TQE' not in GROUNDING_INSTRUCTIONS and 'TO:0000387' not in GROUNDING_INSTRUCTIONS
    from backend.stages.ontology_grounding.rows import parse_rows
    from backend.stages.ontology_grounding.contract import check_response
    wire = '\n'.join(f'{key}.{role}\t{kind}\t{decision}\t-\tDefinition context does not force a mapping.'
        for key in ('C1', 'C2') for role, kind, decision in (
            ('scope', 'scope', 'needs_review'), ('subject', 'concept', 'unresolved'),
            ('object', 'concept', 'unresolved'), ('relation', 'relation', 'unresolved')))
    check_response(parse_rows(wire, data), data)


def test_changed_context_invalidates_cache_and_live_saved_assessments(tmp_path):
    packet, claim, _ = source()
    save_rows(tmp_path / 'extraction_contexts.jsonl', [packet])
    save_rows(tmp_path / 'candidates.jsonl', [claim])
    reference = catalog()
    first = Grounder()
    rows = ground_claims([claim], tmp_path, reference, first)
    assert rows[0]['definition_context_policy'] == POLICY and rows[0]['source_definition_context']
    cached = Grounder(); ground_claims([claim], tmp_path, reference, cached)
    assert not cached.calls
    # File provenance changes, even with an unchanged claim: this must not reuse
    # the old grounding or leave a current-looking browser snapshot.
    with (tmp_path / 'extraction_contexts.jsonl').open('a', encoding='utf-8') as handle:
        handle.write('\n')
    current, stale = current_assertions(tmp_path.parent, reference)
    assert not current and any(r['candidate_id'] == 'one' for r in stale)
    updated = Grounder(); ground_claims([claim], tmp_path, reference, updated)
    assert len(updated.calls) == 1


def test_frozen_baseline_can_exclude_new_context_without_rewriting_claims(tmp_path):
    packet, claim, _ = source()
    before = deepcopy(claim)
    client = Grounder()
    ground_claims([claim], tmp_path, catalog(), client, source_packets=[packet], include_definitions=False)
    assert 'definition_contexts' not in client.calls[0]
    assert claim == before
    row = read_jsonl(tmp_path / 'ontology_grounding.jsonl')[0]
    assert row['definition_context_policy'] == 'disabled'
    assert row['source_definition_context'] == {}
    assert json.dumps(row['original_claim'], sort_keys=True) == json.dumps(before, sort_keys=True)


def test_previous_definition_policy_keeps_saved_results_stale(tmp_path):
    packet, claim, _ = source()
    output = tmp_path / 'paper'
    save_rows(output / 'extraction_contexts.jsonl', [packet])
    save_rows(output / 'candidates.jsonl', [claim])
    reference = catalog()
    rows = ground_claims([claim], output, reference, Grounder())
    current, _ = current_assertions(tmp_path, reference)
    assert any(r['candidate_id'] == 'one' for r in current)
    previous = {**rows[0], 'definition_context_policy': 'document-definition-context-v1'}
    save_rows(output / 'ontology_grounding.jsonl', [previous])
    current, stale = current_assertions(tmp_path, reference)
    assert all(r['candidate_id'] != 'one' for r in current)
    assert any(r['candidate_id'] == 'one' for r in stale)
