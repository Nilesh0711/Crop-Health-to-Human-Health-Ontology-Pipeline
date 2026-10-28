"""Recompute evidence balances from admitted findings; graph structure is not evidence."""
from collections import defaultdict
from copy import deepcopy
import json

from backend.shared.storage import stable_hash, read_jsonl
from backend.stages.external_validation.paper_proofs import guard_paper

POLICY = 'distinct-study-evidence-balance-v1'


def study_key(document):
    """Explicit study grouping wins; DOI/publication identity is a conservative fallback."""
    group = document.get('study_group') or document.get('independence_group')
    if group:
        return str(group)
    doi = str(document.get('doi') or '').lower().strip().removeprefix('https://doi.org/')
    return 'doi:' + doi if doi else str(document['evidence_document_id'])


def evidence_balance(candidate, validation=None, contribution=None):
    validation = validation or {}
    if validation.get('claim_input_signature') not in (None, scientific_signature(candidate)):
        validation = {'validation_status': 'stale_claim_requires_reassessment'}
    findings = {}
    # Each current paper row replaces any earlier version. Repeated packets and
    # reruns cannot manufacture additional votes, and paper origin stays visible.
    for attempt in validation.get('attempts') or []:
        documents = {d['evidence_document_id']: d for d in attempt.get('evidence_documents') or []}
        for row in attempt.get('decisions') or []:
            doc = documents.get(row.get('evidence_document_id'))
            if not doc:
                continue
            checked = guard_paper(candidate, doc, row)
            origin = (checked.get('finding') or {}).get('evidence_origin', 'not_established')
            findings[doc['evidence_document_id']] = {
                'evidence_document_id': doc['evidence_document_id'], 'study_group': study_key(doc),
                'title': doc.get('title'), 'doi': doc.get('doi'), 'decision': checked['decision'],
                'evidence_origin': origin, 'evidence_level': doc.get('evidence_level'),
                'evidence': deepcopy(checked.get('evidence') or []),
                'rationale': checked.get('rationale'), 'guardrail_reasons': checked.get('guardrail_reasons', []),
            }
    groups = defaultdict(set)
    for finding in findings.values():
        # Reviews/background are useful context but not new original study votes.
        if finding['evidence_origin'] == 'original_result' and finding['decision'] in {'supported', 'contradicted'}:
            groups[finding['study_group']].add(finding['decision'])
    support = sorted(g for g, labels in groups.items() if labels == {'supported'})
    conflict = sorted(g for g, labels in groups.items() if labels == {'contradicted'})
    disputed = sorted(g for g, labels in groups.items() if len(labels) > 1)
    has_support = any(f['decision'] == 'supported' for f in findings.values())
    has_conflict = any(f['decision'] == 'contradicted' for f in findings.values())
    status = ('contested' if disputed or (has_support and has_conflict) else
              'supported' if has_support else 'contradicted' if has_conflict else
              'needs_review' if any(f['decision'] == 'needs_review' for f in findings.values()) else
              'insufficient' if findings else 'not_assessed')
    complete = validation.get('validation_status') not in {None, 'pipeline_error', 'external_in_progress', 'retrieval_pending', 'stale_claim_requires_reassessment'}
    return {'policy': POLICY, 'candidate_id': candidate['candidate_id'],
            'score': len(support) - len(conflict), 'score_kind': 'evidence_balance_not_probability',
            'status': status, 'assessment_complete': complete,
            'assessment_status': validation.get('validation_status', 'not_assessed'),
            'source_fidelity': (candidate.get('source_validation') or {}).get('decision', 'not_run'),
            'supporting_study_groups': support, 'contradicting_study_groups': conflict,
            'disputed_study_groups': disputed, 'support_count': len(support), 'contradiction_count': len(conflict),
            'unresolved_count': sum(f['decision'] in {'insufficient', 'needs_review'} for f in findings.values()),
            'findings': list(findings.values()), 'contribution': contribution or {'adjacent_claims': 0, 'two_edge_connections': 0}}


def graph_contributions(graph):
    """Local two-edge connection counts, independent of path pagination or LLM confidence."""
    incoming, outgoing = defaultdict(set), defaultdict(set)
    edges = graph.get('edges') or []
    for edge in edges:
        outgoing[edge['source']].add(edge['id']); incoming[edge['target']].add(edge['id'])
    result = {}
    for edge in edges:
        before = incoming[edge['source']] - {edge['id']}
        after = outgoing[edge['target']] - {edge['id']}
        result[edge['id']] = {'adjacent_claims': len(before | after),
                              'two_edge_connections': len(before) + len(after)}
    return result


def decorate_graph(root, graph):
    contributions = graph_contributions(graph)
    candidates, validations = {}, {}
    for folder in sorted(root.iterdir()) if root.exists() else []:
        if not folder.is_dir() or folder.name.startswith('.'):
            continue
        candidates.update({(folder.name, c['candidate_id']): c for c in read_jsonl(folder / 'candidates.jsonl')})
        validations.update({(folder.name, r['candidate_id']): r for r in read_jsonl(folder / 'validation_runs.jsonl')})
    edges = []
    for edge in graph.get('edges') or []:
        key = (edge['document_key'], edge['candidate_id'])
        if key not in candidates:
            continue
        trust = evidence_balance(candidates[key], validations.get(key), contributions[edge['id']])
        edges.append({**edge, 'trust': trust, 'provisional': True})
    return {**graph, 'edges': edges, 'counts': {'nodes': len(graph.get('nodes') or []), 'edges': len(edges)},
            'trust_policy': POLICY, 'score_is_probability': False}


def connection_assessments(root):
    rows = []
    for folder in sorted(root.iterdir()) if root.exists() else []:
        if not folder.is_dir() or folder.name.startswith('.'):
            continue
        current = {c['candidate_id']: scientific_signature(c) for c in read_jsonl(folder / 'candidates.jsonl')}
        validations = {r['candidate_id']: assessment_fingerprint(r) for r in read_jsonl(folder / 'validation_runs.jsonl')}
        for row in read_jsonl(folder / 'trust_connections.jsonl'):
            ids = (row['upstream_claim_id'], row['downstream_claim_id'])
            if all(cid in current and row.get('claim_signatures', {}).get(cid) == current[cid]
                   and row.get('validation_fingerprints', {}).get(cid) == validations.get(cid) for cid in ids):
                rows.append({**row, 'document_key': folder.name})
    return rows


def scientific_signature(candidate):
    fields = {k: candidate.get(k) for k in ('candidate_id', 'subject', 'relation', 'object', 'subject_type', 'object_type', 'qualifiers',
              'certainty', 'claim_text', 'source_pdf_sha256', 'source_evidence_span', 'source_local_context',
              'source_section_context', 'repair_revision')}
    return stable_hash(json.dumps(fields, sort_keys=True, ensure_ascii=False), 64)


def assessment_fingerprint(result):
    state = {'signature': result.get('validation_signature'), 'status': result.get('validation_status'),
             'attempts': [{'decisions': a.get('decisions'),
                           'semantic': (a.get('joint_assessment') or {}).get('semantic_assessment'),
                           'documents': [{k: d.get(k) for k in ('evidence_document_id', 'doi', 'evidence_chunks')}
                                         for d in a.get('evidence_documents') or []]} for a in result.get('attempts') or []]}
    return stable_hash(json.dumps(state, sort_keys=True, ensure_ascii=False), 64)
