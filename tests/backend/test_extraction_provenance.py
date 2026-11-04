"""Extraction preserves occurrence evidence and honest coverage across packet boundaries."""

from copy import deepcopy
from unittest.mock import patch

import pytest

from backend.stages.claim_extraction.extractor import extract_claims
from tests.backend.test_extraction_targets import claim, packet, row


def test_overlap_merge_keeps_every_evidence_record_and_unique_citation_link(tmp_path):
    pdf = tmp_path / 'source.pdf'
    pdf.write_bytes(b'unchanged source identity')
    text = 'Wheat grains may contain cadmium under disease Z infection [1].'
    source_packet = packet([row(1, text=text)])
    first_reference = {'reference_number': 1, 'citation_key': 'Reference A', 'citation_text': 'Original bibliography text.'}
    second_reference = {'reference_number': 2, 'citation_key': 'Reference B', 'citation_text': 'Additional linked bibliography text.'}
    first_link = {'citation_key': 'Reference A', 'status': 'linked'}
    missing_link = {'citation_key': 'Reference B', 'status': 'missing_bibliography_link'}

    class Client:
        def chat_json(self, *args, **kwargs):
            return {'claims': [claim([1])]}

    events, coverage = [], []
    with patch('backend.stages.claim_extraction.extractor.source_paper_title', return_value='Source paper'), \
            patch('backend.stages.claim_extraction.extractor.link_source_citations', side_effect=[
                ([first_reference], [first_link]),
                ([first_reference, second_reference], [first_link, missing_link]),
            ]):
        candidates, rejected, audit = extract_claims(
            pdf, Client(), 0, lambda *event: events.append(event), bibliography=[],
            source_identifiers={'source_doi': '10.example/source'},
            packets=[source_packet, {**deepcopy(source_packet), 'packet_number': 2}], packet_results=coverage)

    assert len(candidates) == 1
    candidate = candidates[0]
    assert candidate['source_packet_number'] == 1
    assert candidate['source_evidence_span'] == text
    assert len(candidate['source_evidence_records']) == 2
    assert first_reference in candidate['source_evidence_records'][0]['references']
    assert candidate['source_evidence_records'][1]['references'] == [first_reference, second_reference]
    assert candidate['source_references'] == [first_reference, second_reference]
    assert candidate['source_citation_links'] == [first_link, missing_link]
    assert candidate['source_citation_numbers'] == [1]
    assert rejected == {'overlap_occurrence_deduplicated': 1}
    assert audit[0]['reason'] == 'overlap_occurrence_deduplicated'
    assert audit[0]['source_evidence_span'] == text
    assert 'candidate_id' not in audit[0]['proposed_claim']
    assert [record['status'] for record in coverage] == ['completed', 'completed']
    assert any(level == 'warning' and 'Reference B' in message for level, message in events)


@pytest.mark.parametrize('first_reply, expected_status', [
    (RuntimeError('Request timed out'), 'timeout'),
    ({'claims': 'not a list'}, 'failed'),
])
def test_recoverable_packet_failure_keeps_later_findings_without_claiming_full_coverage(tmp_path, first_reply, expected_status):
    pdf = tmp_path / 'source.pdf'
    pdf.write_bytes(b'unchanged source identity')
    source_packet = packet([row(1, text='Wheat grains may contain cadmium under disease Z infection.')])

    class Client:
        calls = 0

        def chat_json(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                if isinstance(first_reply, Exception):
                    raise first_reply
                return first_reply
            return {'claims': [claim([1])]}

    coverage, events = [], []
    with patch('backend.stages.claim_extraction.extractor.source_paper_title', return_value='Source paper'):
        candidates, rejected, audit = extract_claims(
            pdf, Client(), 0, lambda *event: events.append(event), bibliography=[],
            source_identifiers={'source_doi': '10.example/source'},
            packets=[source_packet, {**deepcopy(source_packet), 'packet_number': 2}], packet_results=coverage)

    assert len(candidates) == 1
    assert candidates[0]['source_packet_number'] == 2
    assert [record['status'] for record in coverage] == [expected_status, 'completed']
    assert coverage[0]['error']
    assert coverage[1]['proposed_claims'] == 1
    assert all(record['elapsed_seconds'] >= 0 for record in coverage)
    assert not rejected and not audit
    assert any(level == 'warning' for level, _message in events)
