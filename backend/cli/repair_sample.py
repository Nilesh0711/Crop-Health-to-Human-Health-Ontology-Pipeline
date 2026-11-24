"""Isolated, production-code repair experiments. Preparation/replay never call models.

Real source selections and review expectations belong in the frozen artifact,
never in a production prompt. A run-model command is an explicit paid action.
"""
import argparse
from collections import Counter
from copy import deepcopy
from dataclasses import asdict, replace
import hashlib
import json
from pathlib import Path
import shutil
from statistics import median
from threading import Lock

from backend.config.paths import PROJECT_ROOT
from backend.config.settings import Settings
from backend.domain.abbreviations import packet_definitions
from backend.domain.extraction_review import extraction_warnings, REVIEW_POLICY
from backend.ontology.policies import ENTITY_GROUNDING_POLICY, GROUNDING_POLICY
from backend.shared.storage import file_hash, read_jsonl, stable_hash, utc_now
from backend.stages.claim_extraction.contract import (
    CLOSED_EXTRACTION_CONTRACT_VERSION,
    CLOSED_PREDICATE_SET_VERSION,
    EXTRACTION_CONTRACT_VERSION,
    EXTRACTION_RESPONSE_SCHEMA,
    closed_predicate_whitelist_digest,
)
from backend.stages.claim_extraction.prompt import build_extraction_prompt
from backend.stages.claim_extraction.targeting import build_extraction_targets, targeted_prompt, TARGETING_VERSION, READING_CONTEXT_POLICY
from backend.stages.claim_extraction.anchoring import endpoint_anchor, anchor_diagnostic, ANCHORING_POLICY
from backend.stages.claim_extraction.deduplication import deduplicate_source_claims
from backend.stages.ontology_grounding.contract import ENTITY_SYSTEM, SYSTEM


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + '\n', encoding='utf-8')


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()).hexdigest()


def original_surface(claim, proposed):
    """Undo only recorded source-defined display expansions, never paraphrases."""
    result = deepcopy(proposed)
    for correction in reversed(claim.get('source_name_corrections', [])):
        role = correction.get('field')
        if role in ('subject', 'object') and result.get(role) == correction.get('after'):
            result[role] = correction['before']
    return result


def safe_location(path):
    path = Path(path).resolve()
    protected = [PROJECT_ROOT / name for name in ('ontology', 'deploy', 'archive', 'pdf')]
    if (any(path == p.resolve() or path.is_relative_to(p.resolve()) for p in protected)
            or any(part.casefold() in {'backup', 'backups'} for part in path.parts)):
        raise ValueError('Use an isolated output outside saved results, PDFs, deployments and backups')
    return path


def new_output(path):
    path = safe_location(path)
    if path.exists():
        raise ValueError('Output already exists; prior experiments must not be overwritten')
    path.mkdir(parents=True)
    return path


def policies():
    return {'extraction': EXTRACTION_CONTRACT_VERSION, 'grounding': GROUNDING_POLICY,
            'review': REVIEW_POLICY, 'anchoring': ANCHORING_POLICY,
            'targeting': TARGETING_VERSION, 'reading_context': READING_CONTEXT_POLICY}


def repair_signature():
    from backend.evaluation.readiness import POLICY as READINESS_POLICY
    from backend.ontology.inventory_policy import SIGNATURE as INVENTORY_SIGNATURE
    return digest({'policies': policies(), 'extraction_prompt': build_extraction_prompt(0, 'target_context'),
                   'grounding_inventory': INVENTORY_SIGNATURE,
                   'extraction_schema': EXTRACTION_RESPONSE_SCHEMA, 'grounding_system': SYSTEM,
                   'warning_code': file_hash(PROJECT_ROOT / 'backend/domain/extraction_review.py'),
                   'treatment_label_code': file_hash(PROJECT_ROOT / 'backend/domain/treatment_labels.py'),
                   'definition_context_code': file_hash(PROJECT_ROOT / 'backend/stages/ontology_grounding/source_context.py'),
                   'readiness_code': file_hash(PROJECT_ROOT / 'backend/evaluation/readiness.py'),
                   'readiness': READINESS_POLICY})


def derive_sample(sample_path, output, *, reference=None, controls=None):
    """Explicit new manifest, same frozen source; never edits its parent sample."""
    from backend.ontology.catalog import catalog
    reference = reference or catalog()
    parent = load_sample(sample_path)
    result = deepcopy(parent)
    result['parent_sample_sha256'] = file_hash(Path(sample_path))
    result['parent_sample_path'] = str(Path(sample_path).resolve())
    result['derived_at'] = utc_now()
    result['catalog_signature'] = reference.signature
    result['class_count'] = len(reference.profile['allowed_classes'])
    result['property_count'] = len(reference.profile['allowed_object_properties'])
    result['current_interpretation_policies'] = policies()
    if controls is not None:
        result['retention_controls'] = deepcopy(controls)
    destination = new_output(output)
    write_json(destination / 'sample.json', result)
    write_json(destination / 'sample.sha256.json', {'sha256': file_hash(destination / 'sample.json')})
    return destination / 'sample.json'


def prepare(selection, output):
    """Freeze actual 200-word production targets BEFORE editing instructions."""
    from backend.ontology.catalog import catalog
    from backend.stages.claim_extraction.extractor import source_paper_title
    ref = catalog()
    docs, cases = {}, []
    for document in selection['documents']:
        name, run = document['document'], PROJECT_ROOT / document['run']
        pdf = PROJECT_ROOT / 'pdf' / (name + '.pdf')
        pdf_hash = file_hash(pdf)
        doc_id = 'local-' + stable_hash(f'{pdf.stem}|{pdf_hash}', 32)
        packets = read_jsonl(run / 'extraction_contexts.jsonl')
        definitions = packet_definitions(packets, doc_id)
        targets = build_extraction_targets(packets, 200, abbreviation_glossary=definitions, document_id=doc_id)
        claims = read_jsonl(run / 'candidates.jsonl')
        ground = read_jsonl(run / 'ontology_grounding.jsonl')
        rejections = read_jsonl(run / 'extraction_rejections.jsonl')
        metrics = read_jsonl(run / 'llm_requests.jsonl')
        records = read_jsonl(run / 'extraction_packets.jsonl')
        scope = ref.extraction_instructions()
        system = build_extraction_prompt(0, 'target_context') + '\n\n' + scope
        chosen, selected_claims, selected_rejections = {}, {}, []
        for spec in document['cases']:
            picked = [ground[n - 1] for n in spec.get('records', [])]
            reject = [r for r in rejections if spec.get('rejected_object') and
                      r.get('proposed_claim', {}).get('object') == spec['rejected_object']]
            if spec.get('rejected_object') and not reject:
                raise ValueError('Required rejected proposal not found')
            source_rows = [r['original_claim'] for r in picked]
            if any(c.get('source_pdf_sha256') != pdf_hash for c in source_rows):
                raise ValueError('PDF does not match saved source claims')
            wanted = {c.get('source_target_id') for c in source_rows} | {r.get('target_id') for r in reject}
            matched = [t for t in targets if t['target_id'] in wanted]
            # Reserved old fixtures can have older target identities. Locate their
            # verbatim evidence in current production targets; mark baseline missing.
            if not matched:
                evidence = {f['text'] for c in source_rows for f in c.get('source_evidence_fragments', [])}
                matched = [t for t in targets if any(s['text'] in evidence and
                    n in t['target_sentence_numbers'] for n, s in enumerate(t['sentences'], 1))]
            if not matched:
                raise ValueError('No production target owns this case')
            for target in matched:
                chosen[target['target_id']] = target
            for c in source_rows:
                selected_claims[c['candidate_id']] = c
            selected_rejections.extend(reject)
            cases.append({'case_id': spec['case_id'], 'document': name, 'partition': document['partition'],
                'source_record_numbers': spec.get('records', []),
                'claim_ids': [c['candidate_id'] for c in source_rows],
                'target_ids': [t['target_id'] for t in matched], 'baseline_mappings': picked,
                'baseline_rejections': reject, 'expectations': spec['expectations'],
                'review_status': 'pending', 'reviewed_claims': [], 'review_note': '',
                'baseline_target_compatible': bool(wanted) and wanted <= set(chosen),
                'baseline_policy_compatible': all(r.get('policy') == GROUNDING_POLICY for r in picked)})
        mapping_artifacts = {Path(r.get('response_artifact') or '').name for c in cases
                             if c['document'] == name for r in c['baseline_mappings']}
        selected_metrics = [m for m in metrics if any(t in str(m.get('task', '')) for t in chosen)
            or Path(m.get('response_artifact') or '').name in mapping_artifacts]
        artifacts = {}
        artifact_names = {Path(m.get('response_artifact') or '').name for m in selected_metrics}
        artifact_names |= {Path(r.get('response_artifact') or '').name for c in cases
                           if c['document'] == name for r in c['baseline_mappings']}
        for filename in artifact_names:
            path = run / 'model_responses' / filename
            if filename and path.is_file():
                artifacts[filename] = json.loads(path.read_text(encoding='utf-8'))
        docs[name] = {'pdf_sha256': pdf_hash, 'document_id': doc_id, 'title': source_paper_title(pdf),
            'run': str(run.relative_to(PROJECT_ROOT)), 'abbreviation_definitions': definitions,
            'targets': list(chosen.values()), 'source_packets_sha256': file_hash(run / 'extraction_contexts.jsonl'),
            'baseline_claims': list(selected_claims.values()), 'baseline_rejections': selected_rejections,
            'baseline_metrics': selected_metrics, 'raw_response_artifacts': artifacts,
            'baseline_coverage': [r for r in records if r.get('target_id') in chosen],
            'scope_instructions': scope,
            'extraction_requests': [{'target_id': t['target_id'], 'system': system,
                                    'user': targeted_prompt(t, source_paper_title(pdf))} for t in chosen.values()]}
    frozen = {'purpose': 'Development repair and reserved regression; not an independent accuracy benchmark.',
        'created_at': utc_now(), 'policies': policies(), 'settings': asdict(Settings()),
        'catalog_signature': ref.signature, 'class_count': len(ref.profile['allowed_classes']),
        'property_count': len(ref.profile['allowed_object_properties']),
        'baseline_grounding_system': SYSTEM, 'baseline_extraction_schema': EXTRACTION_RESPONSE_SCHEMA,
        'documents': docs, 'cases': cases}
    destination = new_output(output)
    write_json(destination / 'sample.json', frozen)
    write_json(destination / 'sample.sha256.json', {'sha256': file_hash(destination / 'sample.json')})
    write_json(destination / 'review.json', {'sample_sha256': file_hash(destination / 'sample.json'),
        'cases': [{k: c[k] for k in ('case_id', 'partition', 'expectations', 'review_status',
                                    'reviewed_claims', 'review_note')} for c in cases]})
    return {'cases': len(cases), 'unique_targets': sum(len(d['targets']) for d in docs.values()), 'model_calls': 0}


def load_sample(path):
    path = Path(path)
    frozen = json.loads(path.read_text(encoding='utf-8'))
    recorded = json.loads(path.with_name('sample.sha256.json').read_text(encoding='utf-8'))
    if file_hash(path) != recorded['sha256']:
        raise ValueError('Frozen sample changed; prepare a new experiment instead')
    return frozen


def replay(sample, output):
    """Exercise the SAME materialization/anchor/warning code as production."""
    from backend.stages.claim_extraction.extractor import (
        _DocumentEvidence, _ExtractionResults, _process_packet_claims, finalize_source_context)
    destination = new_output(output)
    target_replays = []
    for name, doc in sample['documents'].items():
        for target in doc['targets']:
            archived = [r for r in doc['raw_response_artifacts'].values() if
                        target['target_id'] in str(r.get('task', '')) and r.get('status') == 'ok']
            if not archived:
                target_replays.append({'document': name, 'target_id': target['target_id'],
                                       'status': 'baseline_missing; explicit baseline model comparison required'})
                continue
            raw = archived[-1].get('accepted_response')
            if not isinstance(raw, dict):
                raw = json.loads(archived[-1]['content'])
            from backend.stages.claim_extraction.extractor import check_extraction_response
            allowed = sorted(set(target['target_sentence_numbers']) | set(target['reading_context_sentence_numbers']))
            check_extraction_response(raw, sample['baseline_extraction_schema'], allowed,
                                      target['target_sentence_numbers'], enforce_target=False)
            state = _ExtractionResults()
            context = _DocumentEvidence(doc['document_id'], doc['pdf_sha256'], doc['title'], {}, None, {}, 'target_context')
            _process_packet_claims(raw['claims'], target, context, state, lambda *a: None)
            finalize_source_context(state.candidates, doc['targets'], doc['document_id'], doc['pdf_sha256'], doc['abbreviation_definitions'])
            target_replays.append({'document': name, 'target_id': target['target_id'], 'status': 'replayed',
                'proposals': len(raw['claims']), 'accepted_claims': state.candidates,
                'rejections': state.rejection_records, 'rejection_counts': dict(state.rejected)})
    results = []
    for case in sample['cases']:
        doc = sample['documents'][case['document']]
        targets = {t['target_id']: t for t in doc['targets']}
        baseline = [c for c in doc['baseline_claims'] if c['candidate_id'] in case['claim_ids']]
        anchors, warnings = [], []
        for claim in baseline:
            proposal = original_surface(claim, claim)
            # Validate the generated wording, BEFORE the production display-name
            # expansion; nested abbreviation expansion is not new anchoring.
            sentences = [s['text'] for s in claim.get('source_evidence_fragments', [])]
            for role in ('subject', 'object'):
                span = claim.get(role + '_source_mention', claim[role])
                match = endpoint_anchor(proposal[role], span, sentences, doc['abbreviation_definitions'])
                anchors.append({'claim_id': claim['candidate_id'], 'role': role, 'supported': bool(match),
                    'category': match[1] if match else anchor_diagnostic(proposal[role], span, sentences)})
            warnings.append({'claim_id': claim['candidate_id'], 'before': claim.get('extraction_warnings', []),
                             'after': extraction_warnings(claim)})
        # Saved raw model proposals are processed by the production binder below;
        # warnings on accepted baselines also expose change without re-generating.
        replayed, failures = [], []
        for rejection in case['baseline_rejections']:
            target = targets[rejection['target_id']]
            item = deepcopy(rejection['proposed_claim'])
            item['evidence_sentence_ids'] = rejection['source_evidence_sentence_numbers']
            item.update({r + '_anchor': rejection['proposed_source_anchors'][r] for r in ('subject', 'object')})
            state = _ExtractionResults()
            context = _DocumentEvidence(doc['document_id'], doc['pdf_sha256'], doc['title'], {}, None, {}, 'target_context')
            _process_packet_claims([item], target, context, state, lambda *a: None)
            replayed.extend(state.candidates)
            failures.extend(state.rejection_records)
        results.append({'case_id': case['case_id'], 'partition': case['partition'], 'anchors': anchors,
            'warnings': warnings, 'replayed_rejections': failures, 'recovered_proposals': replayed,
            'scientific_review': 'pending', 'grounding_assessment': 'not_run'})
    report = {'purpose': sample['purpose'], 'policies': policies(), 'repair_signature': repair_signature(),
        'model_calls': 0, 'cases': results, 'production_target_replays': target_replays,
        'summary': {'cases': len(results), 'anchors_checked': sum(len(c['anchors']) for c in results),
            'unsupported_baseline_anchors': sum(not a['supported'] for c in results for a in c['anchors']),
            'warning_claims': sum(bool(w['after']) for c in results for w in c['warnings']),
            'production_targets_replayed': sum(t['status'] == 'replayed' for t in target_replays),
            'baseline_targets_missing': sum(t['status'] != 'replayed' for t in target_replays),
            'scientifically_assessed': 0}}
    write_json(destination / 'report.json', report)
    return report['summary']


class RecordingClient:
    """Thread-safe request capture around the real provider; no fake model output."""
    def __init__(self, client, destination, *, system_override=None, schema_override=None, label_prefix=None):
        self.client, self.settings = client, client.settings
        self.destination, self.override = destination, system_override
        self.schema_override = schema_override
        self.label_prefix = label_prefix
        self.lock, self.number = Lock(), 0

    def __getattr__(self, name):
        return getattr(self.client, name)

    def _call(self, method, system, user, **options):
        with self.lock:
            self.number += 1
            number = self.number
        system = self.override if self.override is not None else system
        if self.label_prefix and options.get('request_label'):
            options['request_label'] = f'{self.label_prefix} · {options["request_label"]}'
        if self.schema_override is not None and options.get('json_schema'):
            schema = deepcopy(self.schema_override)
            supplied = options['json_schema']['properties']['claims']['items']['properties']['evidence_sentence_ids']
            schema['properties']['claims']['items']['properties']['evidence_sentence_ids'] = deepcopy(supplied)
            options['json_schema'] = schema
        write_json(self.destination / f'input-{number:03d}.json', {'system': system, 'user': user,
            'options': {k: v for k, v in options.items() if k not in {'response_check', 'response_parser'}}})
        return getattr(self.client, method)(system, user, **options)

    def chat_json(self, system, user, **options):
        return self._call('chat_json', system, user, **options)

    def chat_text(self, system, user, **options):
        return self._call('chat_text', system, user, **options)


def run_model(sample, sample_path, output, *, operation, variant, review_path=None, reserved=False,
              case_ids=None, on_event=None, plan_only=False, review_data=None, reference=None, reuse_extraction=None):
    from backend.providers.llm import OpenCodeClient
    from backend.ontology.catalog import catalog
    from backend.shared.artifacts import save_rows
    from backend.stages.claim_extraction.extractor import extract_claims
    from backend.stages.ontology_grounding.grounder import ground_claims
    from backend.stages.ontology_grounding.grouping import evidence_groups
    ref = reference or catalog()
    if ref.signature != sample['catalog_signature']:
        raise ValueError('Vocabulary changed; frozen comparison is incompatible')
    for key in ('anchoring', 'targeting', 'reading_context'):
        if sample['policies'][key] != policies()[key]:
            raise ValueError(f'Frozen source-input policy changed: {key}')
    settings = replace(Settings(), json_repair_attempts=0, json_regeneration_attempts=0)
    frozen_settings = sample['settings']
    for key in ('extraction_model', 'pathway_assembly_model', 'thinking', 'extraction_target_words',
                'extraction_concurrency', 'extraction_min_output_budget', 'ontology_grounding_batch_size',
                'ontology_grounding_workers', 'ontology_grounding_timeout_seconds', 'pathway_assembly_input_characters',
                'ontology_grounding_output_format'):
        if asdict(settings).get(key) != frozen_settings.get(key):
            raise ValueError(f'Semantic comparison settings changed: {key}')
    selected = [c for c in sample['cases'] if (c['partition'] == 'reserved') == reserved]
    if case_ids:
        if not set(case_ids) <= {c['case_id'] for c in selected}:
            raise ValueError('Selected case is unknown or belongs to the other partition')
        selected = [c for c in selected if c['case_id'] in case_ids]
    review = deepcopy(review_data) if review_data is not None else (
        json.loads(Path(review_path).read_text(encoding='utf-8')) if review_path else None)
    if review and review['sample_sha256'] != file_hash(sample_path):
        raise ValueError('Review belongs to a different frozen sample')
    if review and (len(review['cases']) != len(sample['cases']) or
                   {c['case_id'] for c in review['cases']} != {c['case_id'] for c in sample['cases']}):
        raise ValueError('Review must retain every frozen case exactly once')
    from backend.evaluation.readiness import verify_frozen_readiness
    if reserved and (not review or not review.get('frozen_repair_signature') == repair_signature()
            or not verify_frozen_readiness(review, ref, PROJECT_ROOT)['ready']):
        raise ValueError('Reserved comparison requires accepted reviewed development readiness and the frozen repair signature')
    claims_by_doc = {}
    if operation == 'grounding':
        if not review:
            raise ValueError('Fixed grounding requires a separately reviewed claim file')
        for case in selected:
            label = next(c for c in review['cases'] if c['case_id'] == case['case_id'])
            if label['review_status'] not in {'reviewed', 'passed'} or not label.get('review_note'):
                raise ValueError('Grounding inputs must be reviewed, not pending proposed labels')
            if not label['reviewed_claims']:
                raise ValueError('No reviewed claim formulations supplied')
            allowed = {c['candidate_id']: c for c in sample['documents'][case['document']]['baseline_claims']}
            for rejected in case['baseline_rejections']:
                allowed['rejection-' + digest(rejected)[:32]] = {
                    **rejected['proposed_claim'], 'source_target_id': rejected['target_id'],
                    'source_evidence_sentence_numbers': rejected['source_evidence_sentence_numbers'],
                    **{r + '_source_mention': rejected['proposed_source_anchors'][r] for r in ('subject', 'object')}}
            for claim in label['reviewed_claims']:
                # Review may formulate new triples, but must retain frozen provenance.
                parent = allowed.get(claim.get('parent_candidate_id') or claim.get('candidate_id'))
                if parent is None:
                    raise ValueError('Reviewed claim lacks a frozen parent')
                from backend.stages.claim_extraction.extractor import (
                    _DocumentEvidence, _ExtractionResults, _process_packet_claims, check_extraction_response)
                doc = sample['documents'][case['document']]
                target = next((t for t in doc['targets'] if t['target_id'] == parent.get('source_target_id')), None)
                if target is None:
                    # Old reserved records may predate target ownership. Reuse
                    # only the frozen verbatim sentence occurrence, not a fuzzy
                    # text match or an older packet's renumbered IDs.
                    evidence = {f['text'] for f in parent.get('source_evidence_fragments', [])}
                    target = next((t for t in doc['targets'] if t['target_id'] in case['target_ids'] and
                        any(s['text'] in evidence and n in t['target_sentence_numbers']
                            for n, s in enumerate(t['sentences'], 1))), None)
                    if target is None:
                        raise ValueError('Reviewed source does not have a frozen owning target')
                item = original_surface(parent, {k: claim[k] for k in ('subject', 'relation', 'object', 'qualifiers')})
                item['evidence_sentence_ids'] = claim.get('evidence_sentence_ids', parent['source_evidence_sentence_numbers'])
                for role in ('subject', 'object'):
                    item[role + '_anchor'] = claim.get(role + '_anchor', parent.get(role + '_source_mention', parent[role]))
                allowed_ids = sorted(set(target['target_sentence_numbers']) | set(target['reading_context_sentence_numbers']))
                check_extraction_response({'claims': [item]}, EXTRACTION_RESPONSE_SCHEMA,
                                          allowed_ids, target['target_sentence_numbers'])
                context = _DocumentEvidence(doc['document_id'], doc['pdf_sha256'], doc['title'], {}, None, {}, 'target_context')
                state = _ExtractionResults()
                _process_packet_claims([item], target, context, state, lambda *a: None)
                if len(state.candidates) != 1:
                    raise ValueError('Reviewed formulation is not supported by the production source binder')
                from backend.stages.claim_extraction.extractor import finalize_source_context
                finalize_source_context(state.candidates, doc['targets'], doc['document_id'], doc['pdf_sha256'], doc['abbreviation_definitions'])
                copied = state.candidates[0]
                copied['source_abbreviation_definitions'] = deepcopy(doc['abbreviation_definitions'])
                claims_by_doc.setdefault(case['document'], {})[copied['candidate_id']] = copied
    targets_by_doc = {name: [t for t in doc['targets'] if t['target_id'] in
        {tid for c in selected if c['document'] == name for tid in c['target_ids']}]
        for name, doc in sample['documents'].items()}
    reused = {}
    if reuse_extraction:
        if operation != 'combined' or variant != 'repaired':
            raise ValueError('Only current automatic combined flow can reuse extracted outputs')
        for name, targets in targets_by_doc.items():
            if targets:
                reused[name] = checked_extraction_checkpoint(Path(reuse_extraction) / name, {t['target_id'] for t in targets})
                claims_by_doc[name] = {c['candidate_id']: c for c in read_jsonl(Path(reuse_extraction) / name / 'candidates.jsonl')}
    extraction_count = sum(len(t) for t in targets_by_doc.values()) if operation != 'grounding' and not reused else 0
    grounding_count = sum(len(evidence_groups(list(c.values()), 3)) for c in claims_by_doc.values())
    plan = {'planned_extraction_requests': extraction_count,
        'planned_grounding_requests': grounding_count if operation == 'grounding' or reused else
            ('determined after extraction; printed before grounding' if operation == 'combined' else 0),
        'recovery': 'disabled', 'variant': variant}
    for name, targets in targets_by_doc.items():
        if targets and file_hash(PROJECT_ROOT / 'pdf' / (name + '.pdf')) != sample['documents'][name]['pdf_sha256']:
            raise ValueError('Source PDF changed; do not mix it with frozen source inputs')
    if plan_only:
        return plan
    def notify(event):
        if on_event:
            on_event(event)
        elif event.get('type') == 'plan':
            print(json.dumps({k: v for k, v in event.items() if k != 'type'}), flush=True)
    notify({'type': 'plan', **plan})
    destination = new_output(output)
    write_json(destination / 'experiment.json', {'sample_sha256': file_hash(sample_path), 'policies': policies(),
        'repair_signature': repair_signature(),
        'settings': asdict(settings), 'operation': operation, 'variant': variant,
        'selected_case_ids': [c['case_id'] for c in selected],
        'baseline_note': 'Baseline reuses frozen instructions/schema under current shared anchoring and warnings; not an older incompatible cached verdict.',
        'review_sha256': file_hash(review_path) if review_path else None})
    write_json(destination / 'case_inputs.json', [{
        'case_id': c['case_id'], 'document': c['document'], 'target_ids': c['target_ids'],
        'baseline_claims': [r['original_claim'] for r in c['baseline_mappings']],
        'baseline_mappings': c['baseline_mappings'], 'expectations': c['expectations'],
        'baseline_policy_compatible': c['baseline_policy_compatible']} for c in selected])
    events, lock = [], Lock()
    def metrics(row):
        with lock:
            events.append({'at': utc_now(), **row})
            save_rows(destination / 'llm_requests.jsonl', events)
        notify({'type': 'request', **row})
    def emit(level, message):
        notify({'type': 'activity', 'level': level, 'message': str(message)})
    def progress(row):
        notify({'type': 'progress', **row})
    for name, doc in sample['documents'].items():
        if not targets_by_doc[name]:
            continue
        folder = destination / name
        folder.mkdir()
        try:
            from backend.shared.cancellation import check_cancelled
            check_cancelled()
            if operation != 'grounding':
                if name in reused:
                    source = Path(reuse_extraction) / name
                    for path in source.iterdir():
                        if path.is_file() and (path.name in reused[name]['files'] or path.name.startswith('input-')):
                            shutil.copyfile(path, folder / path.name)
                    if (source / 'model_responses').is_dir():
                        shutil.copytree(source / 'model_responses', folder / 'model_responses')
                    claims = list(claims_by_doc[name].values())
                    coverage = json.loads((folder / 'coverage.json').read_text(encoding='utf-8'))
                    write_json(folder / 'reused_extraction.json', {'source':str(source.resolve()),'checkpoint':reused[name]})
                    emit('info', f'Reusing {len(targets_by_doc[name])} verified extraction targets; no new extraction calls')
                else:
                    override = doc['extraction_requests'][0]['system'] if variant == 'baseline' else None
                    client = RecordingClient(OpenCodeClient(settings, task='extraction', on_request=metrics,
                        on_progress=progress, response_dir=folder / 'model_responses'), folder, system_override=override,
                        schema_override=sample['baseline_extraction_schema'] if override else None)
                    coverage = []
                    claims, rejected, rejection_records = extract_claims(PROJECT_ROOT / 'pdf' / (name + '.pdf'),
                        client, 0, emit, packets=targets_by_doc[name],
                        extraction_mode='target_context', target_words=200, prepared_targets=True,
                        scope_instructions=doc['scope_instructions'], bibliography={}, packet_results=coverage)
                    save_rows(folder / 'candidates.jsonl', claims)
                    save_rows(folder / 'extraction_rejections.jsonl', rejection_records)
                    write_json(folder / 'coverage.json', coverage)
                    write_json(folder / 'rejection_counts.json', rejected)
                if any(c['status'] != 'completed' for c in coverage):
                    raise ValueError('Extraction incomplete; technical failure, not scientific unresolved')
                files = {p.name:file_hash(p) for p in folder.iterdir() if p.is_file()
                    and (p.name.startswith('input-') or p.name in {'candidates.jsonl','coverage.json','rejection_counts.json','extraction_rejections.jsonl'})}
                write_json(folder / 'extraction_checkpoint.json', {'files':files,'policies':policies(),
                    'processing_signature': extraction_processing_signature(),
                    'target_ids':sorted(t['target_id'] for t in targets_by_doc[name]),'instruction_variant':variant})
            else:
                claims = list(claims_by_doc.get(name, {}).values())
            if operation in {'grounding', 'combined'} and claims:
                groups = len(evidence_groups(claims, 3))
                check_cancelled()
                notify({'type': 'plan', 'document': name, 'planned_grounding_requests': groups})
                if variant == 'baseline' and settings.ontology_grounding_output_format == 'tsv':
                    from backend.stages.ontology_grounding.contract import JSON_FORMAT
                    from backend.stages.ontology_grounding.rows import FORMAT
                    baseline_system = sample['baseline_grounding_system']
                    if not baseline_system.endswith(JSON_FORMAT):
                        raise ValueError('Frozen baseline JSON format differs; TSV baseline cannot be reconstructed faithfully')
                    override = baseline_system[:-len(JSON_FORMAT)] + FORMAT
                else:
                    override = sample['baseline_grounding_system'] if variant == 'baseline' else None
                ground_folder = folder / 'grounding'
                ground_folder.mkdir()
                client = RecordingClient(OpenCodeClient(settings, task='pathway_assembly', on_request=metrics,
                    on_progress=progress, response_dir=ground_folder / 'model_responses'), ground_folder, system_override=override)
                save_rows(ground_folder / 'candidates.jsonl', claims)
                assessed = ground_claims(claims, ground_folder, ref, client, batch_size=3, workers=5, emit=emit,
                    source_packets=doc['targets'], include_definitions=variant != 'baseline')
                for row in assessed:
                    row['comparison_instruction_variant'] = variant
                    row['supplied_instruction_policy'] = sample['policies']['grounding'] if override else GROUNDING_POLICY
                    row['supplied_system_sha256'] = digest(override or SYSTEM)
                save_rows(ground_folder / 'ontology_grounding.jsonl', assessed)
        except Exception as exc:
            write_json(folder / 'technical_failure.json', {'error': str(exc), 'scientific_status': 'unassessed'})
    return report_experiment(destination)


def lexical_grounding_inputs(cases):
    documents = {}
    for case in cases:
        claims = documents.setdefault(case['document'], {})
        for row in case['baseline_mappings']:
            claim = row['original_claim']
            if not claim.get('schema_error') and not claim.get('superseded_by'):
                claims[claim['candidate_id']] = claim
    return documents


def run_lexical_grounding(sample, cases, output, *, on_event):
    """Evaluate production retrieval on saved source claims in an isolated directory."""
    from backend.config.pipeline import POST_GROUNDING_STAGES_PAUSED
    from backend.domain.classification import classification_counts
    from backend.ontology.catalog import catalog
    from backend.ontology.inventory import open_inventory
    from backend.providers.llm import OpenCodeClient
    from backend.shared.artifacts import save_rows
    from backend.stages.ontology_grounding.grounder import ground_claims

    destination = new_output(output)
    settings = Settings()
    reference, inventory = catalog(), open_inventory()
    groups = lexical_grounding_inputs(cases)
    write_json(destination / 'experiment.json', {
        'operation': 'lexical_grounding', 'catalog_signature': reference.signature,
        'inventory_signature': inventory.signature, 'settings': asdict(settings),
        'selected_case_ids': [case['case_id'] for case in cases], 'claims_per_request': 1, 'workers': 5,
        'post_grounding_stages_paused': POST_GROUNDING_STAGES_PAUSED,
    })
    events, lock, rows = [], Lock(), []

    def request_metrics(row):
        with lock:
            events.append({'at': utc_now(), **row})
            save_rows(destination / 'llm_requests.jsonl', events)
        on_event({'type': 'request', **row})

    for document, claims in groups.items():
        if not claims:
            continue
        source = sample['documents'][document]
        if file_hash(PROJECT_ROOT / 'pdf' / (document + '.pdf')) != source['pdf_sha256']:
            raise ValueError(f'The source PDF changed for {document}')
        folder = destination / document / 'grounding'
        folder.mkdir(parents=True)
        # The request inspector matches responses with metrics by unique label.
        client = RecordingClient(OpenCodeClient(
            settings, task='pathway_assembly', response_dir=folder / 'model_responses',
            on_request=request_metrics, on_progress=lambda event: on_event({'type': 'progress', **event})),
            folder, label_prefix=document)
        candidates = list(claims.values())
        save_rows(folder / 'candidates.jsonl', candidates)
        rows.extend(ground_claims(
            candidates, folder, reference, client, batch_size=1, workers=5, inventory=inventory,
            source_packets=source['targets'],
            emit=lambda level, message: on_event({'type': 'activity', 'level': level, 'message': message})))
    summary = {**classification_counts(rows, reference), 'claims': len(rows), 'requests': len(events),
               'in_scope': sum(row['scope'] == 'in_scope' for row in rows)}
    write_json(destination / 'report.json', summary)
    return summary


def extraction_processing_signature():
    source_root = Path(__file__).resolve().parents[2]
    return digest({name: file_hash(source_root / name) for name in (
        'backend/stages/claim_extraction/extractor.py',
        'backend/domain/extraction_review.py', 'backend/domain/treatment_labels.py')})


def checked_extraction_checkpoint(folder, target_ids):
    """No silent fallback to another extraction if a reused artifact changed."""
    folder = safe_location(folder)
    checkpoint = json.loads((folder / 'extraction_checkpoint.json').read_text(encoding='utf-8'))
    extraction_policies = ('extraction', 'review', 'anchoring', 'targeting', 'reading_context')
    if (checkpoint.get('instruction_variant') != 'repaired'
            or any(checkpoint.get('policies', {}).get(k) != policies()[k] for k in extraction_policies)
            or checkpoint.get('processing_signature') != extraction_processing_signature()
            or set(checkpoint['target_ids']) != set(target_ids)
            or not {'candidates.jsonl','coverage.json'} <= set(checkpoint['files'])
            or any(Path(name).name != name or file_hash(folder / name) != sha for name,sha in checkpoint['files'].items())):
        raise ValueError('Extraction checkpoint changed or is incompatible; it cannot be reused')
    return checkpoint


def closed_extraction_policies():
    return {
        **policies(),
        'extraction': CLOSED_EXTRACTION_CONTRACT_VERSION,
        'predicate_set_version': CLOSED_PREDICATE_SET_VERSION,
        'grounding': ENTITY_GROUNDING_POLICY,
    }


def closed_extraction_processing_signature():
    source_root = Path(__file__).resolve().parents[2]
    return digest({name: file_hash(source_root / name) for name in (
        'backend/stages/claim_extraction/extractor.py',
        'backend/stages/claim_extraction/contract.py',
        'backend/stages/claim_extraction/prompt.py',
        'backend/domain/extraction_review.py',
        'backend/domain/treatment_labels.py',
    )})


def checked_closed_extraction_checkpoint(folder, target_ids, *, reference=None):
    """Verify Stage 1 closed-relation extraction artifacts before Stage 2 entity grounding."""
    from backend.ontology.catalog import catalog

    ref = reference or catalog()
    folder = safe_location(folder)
    checkpoint = json.loads((folder / 'extraction_checkpoint.json').read_text(encoding='utf-8'))
    extraction_policies = ('extraction', 'predicate_set_version', 'review', 'anchoring', 'targeting', 'reading_context')
    expected_policies = closed_extraction_policies()
    expected_prompt = digest(build_extraction_prompt(0, 'target_context', closed_relations=True, reference=ref))
    if (checkpoint.get('operation') != 'closed_extraction'
            or checkpoint.get('instruction_variant') != 'closed_relations'
            or any(checkpoint.get('policies', {}).get(k) != expected_policies[k] for k in extraction_policies)
            or checkpoint.get('predicate_whitelist_sha256') != closed_predicate_whitelist_digest()
            or checkpoint.get('catalog_signature') != ref.signature
            or checkpoint.get('prompt_sha256') != expected_prompt
            or checkpoint.get('processing_signature') != closed_extraction_processing_signature()
            or not set(target_ids) <= set(checkpoint['target_ids'])
            or not {'candidates.jsonl', 'coverage.json'} <= set(checkpoint['files'])
            or any(Path(name).name != name or file_hash(folder / name) != sha for name, sha in checkpoint['files'].items())):
        raise ValueError('Closed-relation extraction checkpoint changed or is incompatible; re-run Stage 1 extraction')
    return checkpoint


def subset_grounding_claims(folder, target_ids):
    return [claim for claim in read_jsonl(folder / 'candidates.jsonl')
            if claim.get('source_target_id') in target_ids
            and claim.get('extraction_review_status') == 'not_flagged'
            and not any(claim.get(key) for key in ('extraction_warnings', 'schema_error', 'superseded_by'))]


def _grounding_source_packets(doc, target_ids):
    recorded_hash = doc.get('source_packets_sha256')
    if recorded_hash and doc.get('run'):
        path = PROJECT_ROOT / doc['run'] / 'extraction_contexts.jsonl'
        if file_hash(path) != recorded_hash:
            raise ValueError('Frozen document definitions changed; verify the source packets before grounding')
        return [{**packet, 'source_document_id': doc['document_id'], 'source_pdf_sha256': doc['pdf_sha256']}
                for packet in read_jsonl(path)]
    return _current_doc_targets(doc, target_ids)


def _current_doc_targets(doc, wanted_ids):
    """Return active targeting/reading-context targets for selected frozen target IDs."""
    frozen_targets = [t for t in doc['targets'] if t['target_id'] in wanted_ids]
    if all(
        t.get('targeting_version') == TARGETING_VERSION
        and t.get('reading_context_policy') == READING_CONTEXT_POLICY
        for t in frozen_targets
    ):
        return frozen_targets
    run_rel = doc.get('run')
    contexts_path = (PROJECT_ROOT / run_rel / 'extraction_contexts.jsonl') if run_rel else None
    if contexts_path is None or not contexts_path.is_file():
        raise ValueError('Frozen target policy is older than current targeting policy and source run extraction_contexts.jsonl is unavailable')
    rebuilt = build_extraction_targets(
        read_jsonl(contexts_path),
        200,
        abbreviation_glossary=doc.get('abbreviation_definitions'),
        document_id=doc.get('document_id'),
    )
    by_id = {t['target_id']: t for t in rebuilt}
    upgraded = []
    for t in frozen_targets:
        rt = by_id.get(t['target_id'])
        if (rt is None
                or rt.get('target_sentence_numbers') != t.get('target_sentence_numbers')
                or rt.get('reading_context_sentence_numbers') != t.get('reading_context_sentence_numbers')
                or [s['text'] for s in rt.get('sentences', ())] != [s['text'] for s in t.get('sentences', ())]):
            raise ValueError(f"Rebuilt v6 target {t['target_id']} does not match frozen target sentences/window")
        rt['frozen_source_pdf_sha256'] = doc['pdf_sha256']
        upgraded.append(rt)
    return upgraded


def _rebuild_doc_targets(doc):
    run_rel = doc.get('run')
    contexts_path = (PROJECT_ROOT / run_rel / 'extraction_contexts.jsonl') if run_rel else None
    if contexts_path is None or not contexts_path.is_file():
        raise ValueError('Source run extraction_contexts.jsonl is unavailable for rebuilding targets')
    rebuilt = build_extraction_targets(
        read_jsonl(contexts_path),
        200,
        abbreviation_glossary=doc.get('abbreviation_definitions'),
        document_id=doc.get('document_id'),
    )
    for rt in rebuilt:
        rt['frozen_source_pdf_sha256'] = doc['pdf_sha256']
    return rebuilt


def _doc_targets_for_ids_or_all(doc, wanted_ids=None):
    """Return active v6 targets for selected target IDs or all targets in the full document."""
    frozen_by_id = {t['target_id']: t for t in doc.get('targets', ())}
    if wanted_ids is None:
        if doc.get('run') and (PROJECT_ROOT / doc['run'] / 'extraction_contexts.jsonl').is_file():
            return _rebuild_doc_targets(doc)
        if frozen_by_id and all(
            t.get('targeting_version') == TARGETING_VERSION
            and t.get('reading_context_policy') == READING_CONTEXT_POLICY
            for t in frozen_by_id.values()
        ):
            return list(doc['targets'])
        return _rebuild_doc_targets(doc)
    wanted = set(wanted_ids)
    if wanted <= set(frozen_by_id):
        return _current_doc_targets(doc, wanted)
    rebuilt = _rebuild_doc_targets(doc)
    by_id = {t['target_id']: t for t in rebuilt}
    missing = sorted(wanted - set(by_id))
    if missing:
        raise ValueError(f'Unknown target ID(s): {", ".join(missing)}')
    return [t for t in rebuilt if t['target_id'] in wanted]


def run_closed_extraction(sample, sample_path, output, *, cases=(), documents=(), target_ids_by_doc=None, on_event=None, reference=None):
    """Run Stage 1 closed-relation extraction on selected subset cases, targets, or full documents."""
    from backend.ontology.catalog import catalog
    from backend.providers.llm import OpenCodeClient
    from backend.shared.artifacts import save_rows
    from backend.stages.claim_extraction.extractor import extract_claims

    settings = Settings()
    ref = reference or catalog()
    selected_docs = list(dict.fromkeys(documents))
    for doc_name in selected_docs:
        if doc_name not in sample['documents']:
            raise ValueError(f'Unknown document key: {doc_name}')
    wanted_by_doc = {}
    doc_order = []
    for case in cases:
        doc_name = case['document']
        if doc_name not in wanted_by_doc:
            doc_order.append(doc_name)
        wanted_by_doc.setdefault(doc_name, set()).update(case['target_ids'])
    for doc_name, tids in (target_ids_by_doc or {}).items():
        if doc_name not in sample['documents']:
            raise ValueError(f'Unknown document key: {doc_name}')
        if tids:
            if doc_name not in wanted_by_doc:
                doc_order.append(doc_name)
            wanted_by_doc.setdefault(doc_name, set()).update(tids)
    for doc_name in selected_docs:
        if doc_name not in wanted_by_doc:
            doc_order.append(doc_name)
        wanted_by_doc[doc_name] = None
    selected_targets = {
        name: _doc_targets_for_ids_or_all(sample['documents'][name], wanted_by_doc[name])
        for name in doc_order if wanted_by_doc[name] is None or wanted_by_doc[name]
    }
    if not selected_targets:
        raise ValueError('No targets selected for closed-relation extraction')
    destination = new_output(output)
    notify = on_event or (lambda event: None)
    write_json(destination / 'experiment.json', {
        'operation': 'closed_extraction',
        'sample_sha256': file_hash(sample_path),
        'catalog_signature': ref.signature,
        'policies': closed_extraction_policies(),
        'predicate_whitelist_sha256': closed_predicate_whitelist_digest(),
        'settings': asdict(settings),
        'selected_case_ids': [case['case_id'] for case in cases],
        'selected_documents': selected_docs,
        'selected_target_ids_by_doc': {k: sorted(v) for k, v in (target_ids_by_doc or {}).items()},
    })
    write_json(destination / 'case_inputs.json', [{
        'case_id': c['case_id'], 'document': c['document'], 'target_ids': c['target_ids'],
        'baseline_claims': [r['original_claim'] for r in c['baseline_mappings']],
        'baseline_mappings': c['baseline_mappings'], 'expectations': c['expectations'],
        'baseline_policy_compatible': c['baseline_policy_compatible'],
    } for c in cases])
    events, lock = [], Lock()
    all_claims = []
    total_duplicates = 0
    prompt_sha = digest(build_extraction_prompt(0, 'target_context', closed_relations=True, reference=ref))

    def request_metrics(row):
        with lock:
            events.append({'at': utc_now(), **row})
            save_rows(destination / 'llm_requests.jsonl', events)
        notify({'type': 'request', **row})

    def emit(level, message):
        notify({'type': 'activity', 'level': level, 'message': str(message)})

    full_doc_set = set(selected_docs)
    for name, targets in selected_targets.items():
        doc = sample['documents'][name]
        if file_hash(PROJECT_ROOT / 'pdf' / (name + '.pdf')) != doc['pdf_sha256']:
            raise ValueError(f'The source PDF changed for {name}')
        folder = destination / name
        folder.mkdir(parents=True)
        try:
            client = RecordingClient(OpenCodeClient(
                settings, task='extraction', on_request=request_metrics,
                on_progress=lambda ev: notify({'type': 'progress', **ev}),
                response_dir=folder / 'model_responses'), folder, label_prefix=name)
            coverage = []
            claims, rejected, rejection_records = extract_claims(
                PROJECT_ROOT / 'pdf' / (name + '.pdf'), client, 0, emit,
                packets=targets, extraction_mode='target_context', target_words=200,
                prepared_targets=True, scope_instructions=doc['scope_instructions'],
                bibliography={}, packet_results=coverage, closed_relations=True, reference=ref,
            )
            canonical_duplicates = 0
            if name in full_doc_set:
                claims, canonical_duplicates = deduplicate_source_claims(claims)
            total_duplicates += canonical_duplicates
            save_rows(folder / 'candidates.jsonl', claims)
            save_rows(folder / 'extraction_rejections.jsonl', rejection_records)
            write_json(folder / 'coverage.json', coverage)
            write_json(folder / 'rejection_counts.json', rejected)
            if any(c['status'] != 'completed' for c in coverage):
                raise ValueError('Closed-relation extraction incomplete; technical failure, not scientific unresolved')
            files = {p.name: file_hash(p) for p in folder.iterdir() if p.is_file()
                     and (p.name.startswith('input-') or p.name in {'candidates.jsonl', 'coverage.json', 'rejection_counts.json', 'extraction_rejections.jsonl'})}
            write_json(folder / 'extraction_checkpoint.json', {
                'operation': 'closed_extraction',
                'instruction_variant': 'closed_relations',
                'files': files,
                'policies': closed_extraction_policies(),
                'catalog_signature': ref.signature,
                'predicate_whitelist_sha256': closed_predicate_whitelist_digest(),
                'prompt_sha256': prompt_sha,
                'processing_signature': closed_extraction_processing_signature(),
                'canonical_duplicates_removed': canonical_duplicates,
                'target_ids': sorted(t['target_id'] for t in targets),
            })
            all_claims.extend(claims)
        except Exception as exc:
            write_json(folder / 'technical_failure.json', {'error': str(exc), 'scientific_status': 'unassessed'})
            raise
    summary = {
        'operation': 'closed_extraction',
        'claims': len(all_claims),
        'held': sum(bool(c.get('extraction_warnings')) for c in all_claims),
        'predicate_counts': dict(Counter(c.get('predicate_alias') or 'none' for c in all_claims)),
        'targets_evaluated': sum(len(ts) for ts in selected_targets.values()),
        'canonical_duplicates_removed': total_duplicates,
        'requests': len(events),
        'technical_failures': len(list(destination.glob('*/technical_failure.json'))),
    }
    write_json(destination / 'report.json', summary)
    return summary


def run_entity_grounding(sample, sample_path, output, *, cases, reuse_extraction, on_event=None, reference=None):
    """Run Stage 2 entity-only grounding over verified Stage 1 closed-relation claims."""
    from backend.config.pipeline import POST_GROUNDING_STAGES_PAUSED
    from backend.domain.classification import classification_counts
    from backend.ontology.catalog import catalog
    from backend.ontology.inventory import open_inventory
    from backend.providers.llm import OpenCodeClient
    from backend.shared.artifacts import save_rows
    from backend.stages.ontology_grounding.grounder import ground_claims
    from backend.stages.ontology_grounding.grouping import CONSISTENCY_POLICY, flag_consistency

    source_root = Path(reuse_extraction)
    settings = Settings()
    ref = reference or catalog()
    inventory = open_inventory()
    targets_by_doc = {}
    for case in cases:
        targets_by_doc.setdefault(case['document'], set()).update(case['target_ids'])
    reused = {}
    claims_by_doc = {}
    source_packets = {}
    for name, target_ids in targets_by_doc.items():
        if not target_ids:
            continue
        doc = sample['documents'][name]
        if file_hash(PROJECT_ROOT / 'pdf' / (name + '.pdf')) != doc['pdf_sha256']:
            raise ValueError(f'The source PDF changed for {name}')
        reused[name] = checked_closed_extraction_checkpoint(source_root / name, target_ids, reference=ref)
        claims_by_doc[name] = subset_grounding_claims(source_root / name, target_ids)
        if claims_by_doc[name]:
            source_packets[name] = _grounding_source_packets(doc, target_ids)
    if not any(claims_by_doc.values()):
        raise ValueError('No unheld saved claims to ground for the selected cases')
    destination = new_output(output)
    notify = on_event or (lambda event: None)
    write_json(destination / 'experiment.json', {
        'operation': 'entity_grounding',
        'sample_sha256': file_hash(sample_path),
        'catalog_signature': ref.signature,
        'inventory_signature': inventory.signature,
        'policies': closed_extraction_policies(),
        'reuse_extraction': str(source_root.resolve()),
        'settings': asdict(settings),
        'selected_case_ids': [case['case_id'] for case in cases],
        'claims_per_request': 1,
        'workers': 5,
        'post_grounding_stages_paused': POST_GROUNDING_STAGES_PAUSED,
        'entity_system_sha256': digest(ENTITY_SYSTEM),
        'consistency_policy': CONSISTENCY_POLICY,
        'source_packets_sha256': {name: digest(packets) for name, packets in source_packets.items()},
    })
    write_json(destination / 'case_inputs.json', [{
        'case_id': c['case_id'], 'document': c['document'], 'target_ids': c['target_ids'],
        'baseline_claims': [r['original_claim'] for r in c['baseline_mappings']],
        'baseline_mappings': c['baseline_mappings'], 'expectations': c['expectations'],
        'baseline_policy_compatible': c['baseline_policy_compatible'],
    } for c in cases])
    events, lock, rows = [], Lock(), []

    def request_metrics(row):
        with lock:
            events.append({'at': utc_now(), **row})
            save_rows(destination / 'llm_requests.jsonl', events)
        notify({'type': 'request', **row})

    def emit(level, message):
        notify({'type': 'activity', 'level': level, 'message': str(message)})

    for name, target_ids in targets_by_doc.items():
        if not target_ids:
            continue
        source_dir = source_root / name
        folder = destination / name
        ground_folder = folder / 'grounding'
        ground_folder.mkdir(parents=True)
        try:
            for item in source_dir.iterdir():
                if item.is_file() and (item.name in reused[name]['files'] or item.name in {'extraction_checkpoint.json', 'rejection_counts.json', 'extraction_rejections.jsonl'}):
                    shutil.copyfile(item, folder / item.name)
            write_json(folder / 'reused_extraction.json', {
                'source': str(source_dir.resolve()),
                'checkpoint': reused[name],
            })
            claims = claims_by_doc[name]
            save_rows(ground_folder / 'candidates.jsonl', claims)
            client = RecordingClient(OpenCodeClient(
                settings, task='pathway_assembly', response_dir=ground_folder / 'model_responses',
                on_request=request_metrics, on_progress=lambda ev: notify({'type': 'progress', **ev})),
                ground_folder, label_prefix=name)
            rows.extend(ground_claims(
                claims, ground_folder, ref, client, batch_size=1, workers=5, inventory=inventory,
                source_packets=source_packets.get(name, ()), emit=emit))
        except Exception as exc:
            write_json(folder / 'technical_failure.json', {'error': str(exc), 'scientific_status': 'unassessed'})
            raise
    before = flag_consistency(deepcopy(rows), reconcile=False)
    current = classification_counts(rows, ref)
    summary = {
        **current,
        'operation': 'entity_grounding',
        'claims': len(rows),
        'requests': len(events),
        'in_scope': sum(r['scope'] == 'in_scope' for r in rows),
        'technical_failures': len(list(destination.glob('*/technical_failure.json'))),
        'consistency_comparison': {
            'before': {
                'classified_claims': classification_counts(before, ref)['classified_claims'],
                'conflict_claims': sum(bool(row.get('mapping_consistency_warnings')) for row in before),
            },
            'after': {
                'classified_claims': current['classified_claims'],
                'conflict_claims': sum(bool(row.get('mapping_consistency_warnings')) for row in rows),
            },
            'adjusted_endpoints': sum(bool(binding.get('consistency_adjustment'))
                                      for row in rows for binding in row['bindings'].values()),
        },
    }
    write_json(destination / 'report.json', summary)
    return summary


def report_experiment(destination):
    destination = safe_location(destination)
    metrics = read_jsonl(destination / 'llm_requests.jsonl')
    claims = []
    for folder in (p for p in destination.iterdir() if p.is_dir()):
        inventory = folder / 'candidates.jsonl'
        claims.extend(read_jsonl(inventory if inventory.exists() else folder / 'grounding/candidates.jsonl'))
    rows = [r for p in destination.glob('*/grounding/ontology_grounding.jsonl') for r in read_jsonl(p)]
    timings = [m['elapsed_seconds'] for m in metrics if m.get('elapsed_seconds') is not None]
    result = {'claims': len(claims), 'held': sum(bool(c.get('extraction_warnings')) for c in claims),
        'grounded_records': len(rows), 'relations': dict(Counter(r['bindings']['relation']['mapping'] for r in rows)),
        'requests': len(metrics), 'technical_failures': len(list(destination.glob('*/technical_failure.json'))),
        'latency_seconds': sum(m.get('elapsed_seconds') or 0 for m in metrics),
        'output_tokens': sum(m.get('output_tokens') or 0 for m in metrics),
        'reasoning_tokens': sum(m.get('reasoning_tokens') or 0 for m in metrics),
        'input_tokens': sum(m.get('input_tokens') or 0 for m in metrics),
        'median_request_seconds': median(timings) if timings else None,
        'request_outcomes': dict(Counter(m.get('status', 'unknown') for m in metrics)),
        'slow_requests': [m for m in metrics if (m.get('elapsed_seconds') or 0) >= 120],
        'truncated_requests': [m for m in metrics if 'truncat' in str(m.get('error', '')).casefold()
                               or m.get('failure_category') == 'incomplete_response'],
        'scientific_review': 'pending; coverage is not an accuracy score'}
    case_file = destination / 'case_inputs.json'
    result['cases'] = []
    for case in json.loads(case_file.read_text(encoding='utf-8')) if case_file.exists() else []:
        folder = destination / case['document']
        inventory = folder / 'candidates.jsonl'
        after = [c for c in read_jsonl(inventory if inventory.exists() else folder / 'grounding/candidates.jsonl')
                 if c.get('source_target_id') in case['target_ids']]
        grounded = read_jsonl(folder / 'grounding/ontology_grounding.jsonl')
        result['cases'].append({'case_id': case['case_id'], 'baseline_claims': case['baseline_claims'],
            'baseline_mappings': case['baseline_mappings'],
            'after_target_claims': after,
            'after_mappings': [r for r in grounded if r['original_claim'].get('source_target_id') in case['target_ids']],
            'expectations': case['expectations'], 'meaning_review': 'pending', 'atomicity_review': 'pending',
            'information_retention_review': 'pending', 'grounding_review': 'pending',
            'technical_failure': (folder / 'technical_failure.json').exists()})
    # Derived reports also never overwrite earlier evaluations.
    report = destination / 'report.json'
    if report.exists():
        raise ValueError('Report exists; choose another experiment instead of overwriting it')
    write_json(report, result)
    lines = ['# Repair experiment', '', 'Scientific review remains pending. Counts are descriptive, not accuracy.', '',
             '| Case | Baseline findings | New target findings | Technical failure | Review |', '|---|---:|---:|---|---|']
    for case in result['cases']:
        lines.append(f"| {case['case_id']} | {len(case['baseline_claims'])} | {len(case['after_target_claims'])} | {case['technical_failure']} | pending |")
    (destination / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    return result


def assess_automatic_experiment(experiment, sample, review, reference=None):
    """Readiness for automatic outputs; corrected formulations never qualify."""
    from backend.evaluation.readiness import output_record, evaluate_readiness
    from backend.ontology.catalog import catalog
    reference = reference or catalog()
    root = safe_location(experiment)
    state = json.loads((root / 'job.json').read_text(encoding='utf-8')) if (root / 'job.json').exists() else {}
    folder = root / 'repaired' if (root / 'repaired').is_dir() else root
    metadata = json.loads((folder / 'experiment.json').read_text(encoding='utf-8'))
    selected = metadata['selected_case_ids']
    partitions = {c['partition'] for c in sample['cases'] if c['case_id'] in selected}
    partition = next(iter(partitions)) if len(partitions) == 1 else 'invalid'
    wanted = {c['case_id'] for c in sample['cases'] if c['partition'] == partition}
    outputs = []
    for path in folder.glob('*/grounding/ontology_grounding.jsonl'):
        outputs += [output_record(path.parent.parent.name, row) for row in read_jsonl(path)]
    report = json.loads((folder / 'report.json').read_text(encoding='utf-8'))
    complete = (metadata['operation'] == 'combined' and metadata['variant'] == 'repaired'
        and metadata.get('repair_signature') == repair_signature() and set(selected) == wanted
        and not report.get('technical_failures') and len(outputs) == report['claims']
        and state.get('status', 'completed') == 'completed')
    assessed = all(c.get('review_status') in {'reviewed','passed','needs_repair'} and c.get('review_note', '').strip()
        for c in review['cases'] if c['case_id'] in wanted)
    return evaluate_readiness(outputs, review.get('output_reviews', {}).get(root.name, []),
        sample.get('retention_controls', []) if partition == 'development' else [], reference,
        coverage_ok=complete, cases_reviewed=assessed, require_retention=partition == 'development')


def _resolve_target_specs(sample, target_specs):
    """Map --target specs ('DOC:target-...' or 'target-...') to document -> set[target_id]."""
    resolved = {}
    rebuilt_ids_cache = {}
    for spec in target_specs:
        if ':' in spec:
            doc_key, tid = spec.split(':', 1)
            if doc_key not in sample['documents']:
                raise ValueError(f'Unknown document key in --target: {doc_key}')
            resolved.setdefault(doc_key, set()).add(tid)
            continue
        matches = [
            doc_key for doc_key, doc in sample['documents'].items()
            if any(t.get('target_id') == spec for t in doc.get('targets', ()))
        ]
        if not matches:
            for doc_key, doc in sample['documents'].items():
                if doc_key not in rebuilt_ids_cache:
                    run_rel = doc.get('run')
                    ctx = (PROJECT_ROOT / run_rel / 'extraction_contexts.jsonl') if run_rel else None
                    rebuilt_ids_cache[doc_key] = (
                        {t['target_id'] for t in _rebuild_doc_targets(doc)}
                        if ctx is not None and ctx.is_file() else set()
                    )
                if spec in rebuilt_ids_cache[doc_key]:
                    matches.append(doc_key)
        if not matches:
            raise ValueError(f'Unknown target ID in --target: {spec}')
        if len(matches) > 1:
            raise ValueError(f'Target ID {spec} matches multiple documents; prefix with DOC_KEY:')
        resolved.setdefault(matches[0], set()).add(spec)
    return resolved


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    p = sub.add_parser('prepare')
    p.add_argument('--selection', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p = sub.add_parser('replay-offline')
    p.add_argument('--sample', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p = sub.add_parser('run-model', help='Explicit paid first-pass comparison; no automatic recovery')
    p.add_argument('--sample', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--operation', choices=('extraction', 'grounding', 'combined'), required=True)
    p.add_argument('--variant', choices=('baseline', 'repaired'), required=True)
    p.add_argument('--review', type=Path)
    p.add_argument('--reserved', action='store_true')
    p.add_argument('--case', action='append', dest='case_ids', help='Optional case ID; repeat to select several cases')
    p = sub.add_parser('report')
    p.add_argument('--experiment', type=Path, required=True)
    p = sub.add_parser('readiness', help='Read-only reviewed automatic-flow assessment; no model calls')
    p.add_argument('--experiment', type=Path, required=True)
    p.add_argument('--sample', type=Path, required=True)
    p.add_argument('--review', type=Path, required=True)
    p = sub.add_parser('closed-extract', help='Explicit Stage 1 closed-relation extraction on subset cases, targets, or full documents')
    p.add_argument('--sample', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--case', action='append', dest='case_ids', default=[])
    p.add_argument('--document', action='append', dest='documents', default=[])
    p.add_argument('--target', action='append', dest='target_specs', default=[])
    p = sub.add_parser('entity-ground', help='Explicit Stage 2 entity-only grounding reusing Stage 1 output')
    p.add_argument('--sample', type=Path, required=True)
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--reuse-extraction', type=Path, required=True)
    p.add_argument('--case', action='append', dest='case_ids', required=True)
    args = parser.parse_args(argv)
    try:
        if args.command == 'prepare':
            result = prepare(json.loads(args.selection.read_text(encoding='utf-8')), args.output)
        elif args.command == 'replay-offline':
            result = replay(load_sample(args.sample), args.output)
        elif args.command == 'run-model':
            result = run_model(load_sample(args.sample), args.sample, args.output, operation=args.operation,
                variant=args.variant, review_path=args.review, reserved=args.reserved, case_ids=args.case_ids)
        elif args.command == 'readiness':
            result = assess_automatic_experiment(args.experiment, load_sample(args.sample),
                json.loads(args.review.read_text(encoding='utf-8')))
        elif args.command == 'closed-extract':
            if not (args.case_ids or args.documents or args.target_specs):
                raise ValueError('Pass at least one --case, --document, or --target')
            sample_obj = load_sample(args.sample)
            by_id = {c['case_id']: c for c in sample_obj['cases']}
            if not set(args.case_ids) <= set(by_id):
                raise ValueError('Unknown case ID in --case')
            selected_cases = [by_id[cid] for cid in dict.fromkeys(args.case_ids)]
            target_ids_by_doc = _resolve_target_specs(sample_obj, args.target_specs) if args.target_specs else None
            result = run_closed_extraction(
                sample_obj, args.sample, args.output,
                cases=selected_cases, documents=args.documents, target_ids_by_doc=target_ids_by_doc,
            )
        elif args.command == 'entity-ground':
            sample_obj = load_sample(args.sample)
            by_id = {c['case_id']: c for c in sample_obj['cases']}
            if not set(args.case_ids) <= set(by_id):
                raise ValueError('Unknown case ID in --case')
            selected_cases = [by_id[cid] for cid in dict.fromkeys(args.case_ids)]
            result = run_entity_grounding(sample_obj, args.sample, args.output, cases=selected_cases,
                                          reuse_extraction=args.reuse_extraction)
        else:
            result = report_experiment(args.experiment)
    except ValueError as exc:
        parser.error(str(exc))
    print(json.dumps({k: v for k, v in result.items() if k != 'cases' or not isinstance(v, list)}, indent=2))
    return 1 if result.get('technical_failures') else 0


if __name__ == '__main__':
    raise SystemExit(main())
