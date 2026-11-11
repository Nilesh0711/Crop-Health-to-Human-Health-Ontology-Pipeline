"""Frozen, first-pass full/local context comparison; never writes live run data."""
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
import json
from statistics import median
import time

from backend.config.paths import ONTOLOGY_DIR
from backend.shared.artifacts import save_json, save_rows
from backend.shared.storage import read_jsonl, stable_hash, utc_now
from backend.shared.text import source_surface_mention
from backend.stages.claim_extraction.contract import EXTRACTION_RESPONSE_SCHEMA, EXTRACTION_CONTRACT_VERSION
from backend.stages.claim_extraction.extractor import check_extraction_response, evidence_span_from_item
from backend.stages.claim_extraction.prompt import build_extraction_prompt
from backend.stages.claim_extraction.targeting import build_extraction_targets, targeted_prompt, TARGETING_VERSION


def prepare_comparison(packets, reference, output, *, targets, source_title, document_id='comparison', target_words=800):
    if output.resolve().is_relative_to(ONTOLOGY_DIR.resolve()):
        raise ValueError('Extraction comparisons must be outside live ontology results')
    if output.exists() and any(output.iterdir()):
        raise ValueError('Comparison directory is not empty; preserve previous experiments')
    all_targets = build_extraction_targets(packets, target_words, document_id=document_id)
    if not targets or len(targets) > 10 or len(set(targets)) != len(targets) or any(n < 1 or n > len(all_targets) for n in targets):
        raise ValueError('Choose 1–10 distinct existing extraction target numbers')
    system = build_extraction_prompt(0, 'target_context') + '\n\n' + reference.extraction_instructions()
    frozen = []
    for number in targets:
        target = all_targets[number - 1]
        schema = deepcopy(EXTRACTION_RESPONSE_SCHEMA)
        schema['properties']['claims']['items']['properties']['evidence_sentence_ids']['items']['enum'] = target['target_sentence_numbers']
        frozen.append({'number': number, 'target': target, 'system': system, 'schema': schema,
            'user': {mode: targeted_prompt(target, source_title, context_mode=mode) for mode in ('full', 'local')}})
    save_rows(output / 'inputs.jsonl', frozen)
    metadata = {'created_at': utc_now(), 'catalog_signature': reference.signature, 'target_words': target_words,
        'targeting_version': TARGETING_VERSION, 'extraction_contract_version': EXTRACTION_CONTRACT_VERSION,
        'input_signature': stable_hash(json.dumps(frozen, ensure_ascii=False, sort_keys=True), 64),
        'targets': targets, 'source_title': source_title, 'live_results_modified': False}
    save_json(output / 'sample.json', metadata)
    return metadata


def run_comparison(output, client_factory, *, workers=2, emit=lambda message: None):
    if workers not in (1, 2):
        raise ValueError('Use one or two comparison workers')
    cases = read_jsonl(output / 'inputs.jsonl')
    if not cases or any((output / 'results').glob('*.json')):
        raise ValueError('Prepare a new frozen comparison before running it')

    def assess(case, mode):
        metrics = []
        label = f'target-{case["number"]}-{mode}'
        client = client_factory(metrics.append, output / 'responses' / label)
        if client.settings.json_regeneration_attempts or client.settings.json_repair_attempts:
            raise ValueError('Comparison requires recovery disabled to measure first-pass results')
        if hasattr(client, 'set_request_context'):
            client.set_request_context({'stage': 'extraction', 'extraction_contract_version': EXTRACTION_CONTRACT_VERSION,
                'targeting_version': TARGETING_VERSION, 'context_comparison': mode})
        emit(f'Starting {label}')
        started, answer, error = time.monotonic(), None, None
        target = case['target']
        def check(response):
            check_extraction_response(response, case['schema'], target['target_sentence_numbers'])
        try:
            answer = client.chat_json(case['system'], case['user'][mode], json_schema=case['schema'],
                max_tokens=min(12000, max(4000, target['target_word_count'] * 8)),
                response_check=check, request_label=f'extraction context comparison {label}')
            check(answer)
        except (RuntimeError, ValueError) as exc:
            answer, error = None, str(exc)
        claims = (answer or {}).get('claims', [])
        unanchored = []
        for index, claim in enumerate(claims):
            evidence = evidence_span_from_item(claim, target['sentences'])
            if evidence is None or any(not source_surface_mention(evidence[2], claim[role]) for role in ('subject', 'object')):
                unanchored.append(index)
        result = {'number': case['number'], 'mode': mode, 'model': client.model,
            'thinking': client.settings.task_thinking['extraction'],
            'generation_budget': client.settings.extraction_min_output_budget,
            'target_word_count': target['target_word_count'], 'input_characters': len(case['system']) + len(case['user'][mode]),
            'valid_first_pass': answer is not None and error is None, 'error': error, 'answer': answer,
            'unanchored_claim_indexes': unanchored, 'elapsed_seconds': round(time.monotonic() - started, 3), 'requests': metrics}
        save_json(output / 'results' / (label + '.json'), result)
        emit(f'Finished {label}: {"valid" if result["valid_first_pass"] else error}; {result["elapsed_seconds"]}s; {len(claims)} claims')
        return result

    # Reverse the ordering of alternate pairs; provider load/caching still affect timing.
    work = [(case, mode) for i, case in enumerate(cases) for mode in (('full', 'local') if i % 2 == 0 else ('local', 'full'))]
    results = []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(assess, case, mode) for case, mode in work]
        for future in as_completed(futures):
            results.append(future.result())
    configurations = {(r['model'], r['thinking'], r['generation_budget']) for r in results}
    if len(configurations) != 1:
        raise ValueError('Comparison model, Thinking or generation budget differed; inspect isolated results')
    report = {'created_at': utc_now(), 'live_results_modified': False, 'recovery_disabled': True,
        'scientific_accuracy_evaluated': False, 'summary': {}, 'differences': [],
        'notes': ['Only reading context differs: both modes use the same target, rules, class names, source abbreviation definitions and response schema.',
                  'Unchanged sentence IDs and literal endpoint anchors are checked. This is not scientific gold evaluation.',
                  'Compare qualifiers and omitted/added records manually; lower token use alone does not prove quality.',
                  'Full original packets are frozen. Timing depends on provider load, reasoning and caching.']}
    for mode in ('full', 'local'):
        group = [r for r in results if r['mode'] == mode]
        requests = [m for r in group for m in r['requests']]
        usage = [m for m in requests if m.get('usage_reported')]
        report['summary'][mode] = {'targets': len(group), 'valid_first_pass': sum(r['valid_first_pass'] for r in group),
            'claims': sum(len((r['answer'] or {}).get('claims', [])) for r in group),
            'unanchored_claims': sum(len(r['unanchored_claim_indexes']) for r in group),
            'median_seconds': round(median(r['elapsed_seconds'] for r in group), 3),
            'reported_requests': len(usage), **{k: sum(m.get(k) or 0 for m in usage) if usage else None
                for k in ('input_tokens', 'output_tokens', 'reasoning_tokens')}}
    for case in cases:
        pair = {r['mode']: r for r in results if r['number'] == case['number']}
        records = {mode: {json.dumps(row, sort_keys=True, ensure_ascii=False) for row in
            (pair[mode]['answer'] or {}).get('claims', [])} for mode in ('full', 'local')}
        report['differences'].append({'number': case['number'], 'both_valid': all(r['valid_first_pass'] for r in pair.values()),
            'identical_records': len(records['full'] & records['local']),
            'only_full': [json.loads(s) for s in sorted(records['full'] - records['local'])],
            'only_local': [json.loads(s) for s in sorted(records['local'] - records['full'])]})
    save_json(output / 'comparison.json', report)
    return report
