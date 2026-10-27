"""Prevent unbalanced experiment plans and inappropriate source-verdict reuse."""
from collections import Counter
from copy import deepcopy
import hashlib
import json

import pytest

from scripts.research.packet_comparison.compare import make_plan, job_key, run
from scripts.research.packet_comparison.audit import candidate_key, transport_failed


def test_plan_is_fixed_balanced_and_contains_every_packet_once_per_repeat():
    cases=[dict(id='article',arms=dict(current=[dict(packet_number=i) for i in range(1,4)],
                                     small=[dict(packet_number=i) for i in range(1,6)])),
           dict(id='chapter',arms=dict(current=[dict(packet_number=1)],
                                     small=[dict(packet_number=i) for i in range(1,3)]))]
    plan=make_plan(cases,2,24102026)
    assert plan==make_plan(cases,2,24102026)
    assert len(plan)==22
    assert len({job_key(j) for j in plan})==22
    counts=Counter((j['case'],j['arm'],j['repeat']) for j in plan)
    for case in cases:
        for arm,packets in case['arms'].items():
            for repeat in (1,2):
                assert counts[case['id'],arm,repeat]==len(packets)
    assert {j['repeat'] for j in make_plan(cases,1,24102026,first_repeat=3)}=={3}


def test_audit_cache_cannot_reuse_judgement_for_different_conditions_or_context():
    candidate=dict(subject='cadmium',relation='reduces',object='microbial activity',
                   qualifiers=dict(condition='high levels in soil'),
                   source_evidence_span='High levels reduce activity.',
                   source_local_context='The preceding sentence defines the soil exposure.',
                   source_context_packet='The full original paragraph.')
    original=candidate_key(candidate)
    for field,value in [('qualifiers',dict(condition='low levels in soil')),
                        ('source_local_context','A different preceding sentence.'),
                        ('source_context_packet','A different paragraph.'),
                        ('relation','increases')]:
        changed=deepcopy(candidate)
        changed[field]=value
        assert candidate_key(changed)!=original
    assert candidate_key(dict(reversed(list(candidate.items()))))==original


def test_changed_pdf_stops_before_paid_work_or_plan_changes(tmp_path):
    pdf=tmp_path/'source.pdf'
    pdf.write_bytes(b'changed scientific source')
    (tmp_path/'case.prepared.json').write_text(json.dumps(dict(id='case',pdf=str(pdf))))
    (tmp_path/'pdf_fingerprints.json').write_text(json.dumps(dict(case='original-file-digest')))
    with pytest.raises(ValueError,match='Source PDF changed'):
        run(tmp_path)
    assert not (tmp_path/'manifest.json').exists()
    assert not (tmp_path/'extraction').exists()


def test_changed_reference_targets_cannot_redefine_a_running_experiment(tmp_path):
    pdf=tmp_path/'source.pdf'
    pdf.write_bytes(b'immutable source')
    digest=hashlib.sha256(pdf.read_bytes()).hexdigest()
    (tmp_path/'case.prepared.json').write_text(json.dumps(dict(id='case',pdf=str(pdf))))
    (tmp_path/'pdf_fingerprints.json').write_text(json.dumps(dict(case=digest)))
    (tmp_path/'reference_targets.json').write_text('[]')
    (tmp_path/'scientific_inputs.json').write_text(json.dumps(dict(
        pdf_sha256=dict(case=digest),reference_targets_sha256='earlier-reference-targets')))
    with pytest.raises(ValueError,match='Scientific inputs changed'):
        run(tmp_path)
    assert not (tmp_path/'manifest.json').exists()
    assert not (tmp_path/'extraction').exists()


def test_remeasurement_cannot_seek_a_more_favorable_scientific_verdict():
    assert transport_failed(dict(decision='audit_error',rationale='OpenCode inference connection failed (ReadError)'))
    assert transport_failed(dict(decision='audit_error',rationale='OpenCode inference HTTP 503: unavailable'))
    for decision in ['unsupported','needs_review','entailed']:
        assert not transport_failed(dict(decision=decision,rationale='Request timed out in an explanation'))
    assert not transport_failed(dict(decision='audit_error',rationale='Response parsing failed'))
    assert not transport_failed(dict(decision='audit_error',rationale='OpenCode inference HTTP 402: billing limit'))
