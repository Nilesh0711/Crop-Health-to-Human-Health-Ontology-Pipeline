"""Reviewed development acceptance, distinct from application mapping counts.

No model calls, scientific rewriting, identity sharing or production admission.
"""
import json
from collections import defaultdict
from pathlib import Path

from backend.domain.classification import (
    binding_classified,
    classification_counts,
    endpoint_accepted,
    relation_property_type_status,
    row_mapping_warnings,
)
from backend.shared.storage import stable_hash

POLICY = 'reviewed-subset-readiness-v1'
ROLES = ('subject', 'relation', 'object')


def output_record(document, row):
    return {'id': 'output-' + stable_hash(document + '|' + row['candidate_id'], 32),
        'signature': stable_hash(json.dumps(row, sort_keys=True, ensure_ascii=False, default=str), 64),
        'document': document, 'row': row, 'claim': row['original_claim']}


def _reviewed(output, reviews):
    label = reviews.get(output['id'], {})
    if label.get('signature') != output['signature']:
        return {}
    return label


def _held(row, label, reference=None):
    return (label.get('disposition') in {'hold', 'reject'}
        or row.get('extraction_review_status') == 'rejected'
        or row.get('extraction_review_status') == 'held' and label.get('disposition') != 'accept_extraction'
        or bool(row.get('extraction_warnings')) and label.get('disposition') != 'accept_extraction'
        or bool(row.get('mapping_consistency_warnings'))
        or bool(row_mapping_warnings(row, reference)))


def evaluate_readiness(outputs, reviews, controls, reference, *, coverage_ok=False,
                       cases_reviewed=False, require_retention=True, _group_documents=True):
    """Keep original in-scope denominators, including held/rejected outputs."""
    reviews = {r['output_id']: r for r in reviews}
    included = [o for o in outputs if o['row'].get('scope') == 'in_scope']
    pending, entity_count, whole_count, non_atomic, serious_unheld, unheld_review, held = 0, 0, 0, 0, 0, 0, 0
    found = set()
    for output in outputs:
        row, claim = output['row'], output['claim']
        label = _reviewed(output, reviews)
        complete_review = (label.get('scope_confirmed') is True and bool(label.get('note', '').strip())
            and label.get('atomicity') in {'atomic', 'valid_joint', 'bundled', 'uncertain'}
            and label.get('meaning') in {'faithful', 'serious_error', 'uncertain'}
            and all(label.get('mappings', {}).get(role) in {'justified', 'questionable', 'incorrect'} for role in ROLES))
        if not complete_review or row.get('scope') not in {'in_scope', 'out_of_scope'}:
            pending += 1
        if row.get('scope') != 'in_scope':
            continue
        hold = _held(row, label, reference)
        held += bool(hold)
        atomic = label.get('atomicity') in {'atomic', 'valid_joint'}
        non_atomic += not atomic  # Unassessed atomicity cannot help a threshold.
        serious_unheld += label.get('meaning') == 'serious_error' and not hold
        unheld_review += bool(label) and not hold and (not atomic or label.get('meaning') == 'uncertain'
            or any(label.get('mappings',{}).get(role) in {'questionable','incorrect'} for role in ROLES))
        rel_pt_status = relation_property_type_status(row, reference)
        mapped = {
            role: (
                label.get('mappings', {}).get(role) == 'justified'
                and (
                    endpoint_accepted(row, role, reference)
                    if role in ('subject', 'object')
                    else (
                        binding_classified(row.get('bindings', {}).get('relation', {}), 'relation', reference)
                        and not row.get('bindings', {}).get('relation', {}).get('under_review')
                        and rel_pt_status not in {'incompatible', 'missing_required_endpoint'}
                    )
                )
            )
            for role in ROLES
        }
        entity_count += sum(mapped[role] for role in ('subject', 'object'))
        whole_count += bool(complete_review and not hold and atomic and label.get('meaning') == 'faithful' and all(mapped.values()))
        # Retention is source-information coverage, independent of mapping
        # eligibility/atomicity. A faithful held conjunction retains its named
        # findings but still counts against atomicity and never completes a claim.
        if (complete_review and label.get('meaning') == 'faithful'
                and label.get('disposition') != 'reject' and row.get('extraction_review_status') != 'rejected'):
            for control in controls:
                if (control['id'] in label.get('retained_findings', [])
                        and control['document'] == output['document']
                        and claim.get('source_target_id') in control['target_ids']
                        and (not control.get('source_pdf_sha256') or control['source_pdf_sha256'] == claim.get('source_pdf_sha256'))
                        and set(control['sentence_ids']).intersection(claim.get('source_evidence_sentence_numbers', []))):
                    found.add(control['id'])
    total = len(included)
    thresholds = {'entity': bool(total and entity_count * 100 >= 70 * total * 2),
                  'whole_claim': bool(total and whole_count * 100 >= 40 * total),
                  'atomicity': bool(total and non_atomic * 100 < 20 * total)}
    missing = sorted({c['id'] for c in controls} - found)
    reasons = []
    if not coverage_ok:
        reasons.append('Complete compatible automatic-flow coverage is required; fixed reviewed formulations cannot supply readiness rates.')
    if not cases_reviewed:
        reasons.append('Every source case needs a review note and an assessed status; residual failures need not be relabelled passed.')
    if not outputs or not total:
        reasons.append('No assessable in-scope automatic outputs.')
    if pending:
        reasons.append(f'{pending} outputs have missing, stale or pending reviews/scope decisions.')
    if require_retention and not controls:
        reasons.append('The frozen retained-finding controls are missing.')
    if missing:
        reasons.append(f'{len(missing)} required source findings are missing or not faithfully anchored.')
    if serious_unheld or unheld_review:
        reasons.append('Known serious errors, uncertain meanings, residual bundles and questionable mappings must be held.')
    for key, passed in thresholds.items():
        if not passed:
            reasons.append({'entity': 'Justified entity classification must reach 70%.',
                'whole_claim': 'Justified complete-claim classification must reach 40%.',
                'atomicity': 'Incorrect/unresolved atomicity must remain below 20%.'}[key])
    per_document = {}
    if _group_documents:
        for document in sorted({o['document'] for o in outputs}):
            item = evaluate_readiness([o for o in outputs if o['document'] == document], list(reviews.values()),
                [c for c in controls if c['document'] == document], reference, coverage_ok=coverage_ok,
                cases_reviewed=cases_reviewed, require_retention=require_retention, _group_documents=False)
            per_document[document] = {k:item[k] for k in ('ready','claims','entity_rate','whole_claim_rate','non_atomic_rate','reasons')}
            if not item['ready'] and len({o['document'] for o in outputs}) > 1:
                reasons.append(f'{document} does not meet its own acceptance criteria; PDFs cannot be pooled to pass.')
    rate = lambda n, d: n / d if d else None
    return {'policy': POLICY, 'label': 'Reviewed subset; AI-assisted development evaluation, not independent accuracy',
        'state': 'accepted_with_limitations' if not reasons else 'not_ready', 'ready': not reasons,
        'claims': total, 'endpoint_mentions': total * 2, 'classified_endpoints': entity_count,
        'classified_claims': whole_count, 'non_atomic_claims': non_atomic,
        'entity_rate': rate(entity_count, total * 2), 'whole_claim_rate': rate(whole_count, total),
        'non_atomic_rate': rate(non_atomic, total), 'held': held, 'pending_reviews': pending,
        'serious_unheld': serious_unheld, 'unheld_review_issues': unheld_review,
        'retained_findings': len(found), 'required_findings': len(controls), 'missing_findings': missing,
        'thresholds': thresholds, 'reasons': reasons, 'per_document': per_document}


def application_audit_plan(rows, document, reference, limit=30):
    """All flagged + deterministic stratified unflagged; never accuracy estimates."""
    outputs = [output_record(document, row) for row in rows]
    flagged = [o for o in outputs if o['row'].get('extraction_warnings')
        or o['row'].get('mapping_consistency_warnings') or o['row'].get('scope') == 'needs_review'
        or o['row'].get('extraction_review_status') in {'held', 'rejected'}
        or relation_property_type_status(o['row'], reference) in {'incompatible', 'missing_required_endpoint'}
        or bool(row_mapping_warnings(o['row'], reference))]
    ids = {o['id'] for o in flagged}
    buckets = defaultdict(list)
    for output in outputs:
        if output['id'] not in ids and output['row'].get('scope') == 'in_scope':
            category = output['row']['bindings']['relation'].get('mapping')
            buckets[category if category in {'exact', 'broader'} else 'unresolved'].append(output)
    key = lambda o: stable_hash(document + '|' + o['claim']['candidate_id'], 64)
    chosen = []
    for category in ('exact', 'broader', 'unresolved'):
        chosen += sorted(buckets[category], key=key)[:limit // 3]
    chosen_ids = {o['id'] for o in chosen}
    remainder = sorted([o for items in buckets.values() for o in items if o['id'] not in chosen_ids], key=key)
    chosen += remainder[:max(0, limit - len(chosen))]
    return {'label': 'Application-classified counts; audit sample is not verified whole-PDF accuracy',
        'policy': POLICY, 'document': document, 'application_counts': classification_counts(rows, reference),
        'in_scope_claims': sum(r.get('scope') == 'in_scope' for r in rows),
        'flagged': flagged, 'unflagged': chosen, 'unflagged_audit_limit': limit}


def audit_review_status(plan, reviews):
    by_id = {r['output_id']: r for r in reviews}
    pending, bad, serious = 0, 0, 0
    sample_ids = {o['id'] for o in plan['unflagged']}
    for output in plan['flagged'] + plan['unflagged']:
        label = _reviewed(output, by_id)
        if (not label.get('note') or label.get('atomicity') not in {'atomic', 'valid_joint', 'bundled', 'uncertain'}
                or label.get('meaning') not in {'faithful','serious_error','uncertain'}
                or not label.get('scope_confirmed')
                or any(label.get('mappings',{}).get(r) not in {'justified','questionable','incorrect'} for r in ROLES)):
            pending += 1
        if output['id'] in sample_ids:
            bad += label.get('atomicity') not in {'atomic', 'valid_joint'}
        serious += label.get('meaning') == 'serious_error' and not _held(output['row'], label)
    total = len(sample_ids)
    return {'label': 'Observed audit only; do not extrapolate to whole-PDF accuracy',
        'reviewed_unflagged_sample': total, 'non_atomic_in_sample': bad, 'pending_reviews': pending,
        'serious_unheld': serious, 'ready': bool(total and not pending and not serious and bad * 100 < total * 20)}


def verify_frozen_readiness(review, reference, project_root):
    """Recalculate from content-addressed automatic artifacts, not a saved flag."""
    from backend.shared.storage import read_jsonl, file_hash
    frozen = review.get('frozen_evaluation') or {}
    if (frozen.get('catalog_signature') != reference.signature or frozen.get('policy') != POLICY
            or not frozen.get('files')):
        return {'ready': False, 'reasons': ['Frozen reviewed evaluation is missing or incompatible.']}
    outputs = []
    for item in frozen['files']:
        path = (Path(project_root) / item['path']).resolve()
        if not path.is_relative_to((Path(project_root) / 'analysis').resolve()) or file_hash(path) != item['sha256']:
            return {'ready': False, 'reasons': ['Frozen automatic artifacts changed or are outside the experiment area.']}
        outputs += [output_record(item['document'], row) for row in read_jsonl(path)]
    return evaluate_readiness(outputs, frozen.get('reviews', []), frozen.get('controls', []), reference,
        coverage_ok=True, cases_reviewed=True)
