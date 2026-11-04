"""Read and recheck saved evidence from historical claim-validation runs."""
from typing import Any

from backend.stages.external_validation.proofs import decision_errors


def explicit_conflict(candidate, decision):
    """Require an explicit model-declared incompatibility, not a lexical classifier.

    Callers also validate paper/chunk IDs, exact quotes, endpoint bindings and
    conditions. This checks response consistency for any relation vocabulary;
    the evidence model owns the semantic judgement.
    """
    matches = decision.get('matches') or {}
    return (matches.get('subject') is True and matches.get('object') is True
            and (matches.get('relation') is False or matches.get('direction') is False)
            and bool(str(decision.get('rationale') or '').strip())
            and any(e.get('stance') == 'contradicts' and e.get('quoted_span')
                    and e.get('relation_trigger') for e in decision.get('evidence', [])))


def verified_contradictions(candidate: dict[str, Any], validation: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Only saved, quote-validated contradictions can enable external repair."""
    verified = []
    if not validation or validation.get('validation_status') in {'pipeline_error', 'external_in_progress'}:
        return verified
    for attempt in validation.get('attempts', []):
        packet = {'candidate': candidate, 'evidence_documents': attempt.get('evidence_documents', [])}
        for decision in attempt.get('decisions', []):
            if decision.get('proof_policy') in {'paper-local-context-v1', 'paper-local-context-v2', 'paper-local-context-v3'}:
                from backend.stages.external_validation.paper_proofs import guard_paper
                doc = next((d for d in packet['evidence_documents'] if d['evidence_document_id'] == decision.get('evidence_document_id')), None)
                if doc and decision.get('decision') == 'contradicted' and guard_paper(candidate, doc, decision)['decision'] == 'contradicted':
                    verified.append(decision)
                continue
            if (decision.get('decision') == 'contradicted' and decision.get('evidence')
                    and not decision_errors(packet, decision) and explicit_conflict(candidate, decision)):
                verified.append(decision)
    return verified


from backend.shared.storage import read_jsonl
from backend.domain.claim_signatures import scientific_signature, assessment_fingerprint


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
