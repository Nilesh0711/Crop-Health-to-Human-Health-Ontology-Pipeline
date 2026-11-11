"""Isolated first-pass comparison of object claims and fixed-position JSON rows."""
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
import json
from statistics import median
import time

from backend.shared.artifacts import save_json
from backend.shared.storage import read_jsonl, utc_now
from backend.shared.text import source_surface_mention
from backend.stages.claim_extraction import row_format
from backend.stages.claim_extraction.extractor import check_extraction_response, evidence_span_from_item


def run_comparison(output, client_factory, *, emit=lambda message: None):
    cases = read_jsonl(output / 'inputs.jsonl')
    if not cases or (output / 'format_comparison.json').exists() or any((output / 'format_results').glob('*.json')):
        raise ValueError('Prepare a new frozen comparison before running it')

    def assess(pair):
        case, mode = pair
        label = f'target-{case["number"]}-{mode}'
        metrics = []
        client = client_factory(metrics.append, output / 'responses' / label)
        if client.settings.json_regeneration_attempts or client.settings.json_repair_attempts:
            raise ValueError('Disable recovery for first-pass comparisons')
        allowed = case['target']['target_sentence_numbers']
        schema = deepcopy(case['schema'])
        # The comparison is about serialization, not whether type hints help.
        fields = schema['properties']['claims']['items']['properties']
        fields.pop('subject_type', None)
        fields.pop('object_type', None)
        system = case['system'] + '\nFor this comparison omit optional subject_type and object_type hints.'
        if mode == 'rows':
            schema, system = row_format.response_schema(allowed), row_format.prompt(case['system'])
        def normalized(value):
            return row_format.decode(value, allowed) if mode == 'rows' else value
        def check(value):
            check_extraction_response(normalized(value), case['schema'], allowed)
        started = time.monotonic()
        answer, wire, error = None, None, None
        emit(f'Starting {label}')
        try:
            wire = client.chat_json(system, case['user']['local'], json_schema=schema,
                max_tokens=min(12000, max(4000, case['target']['target_word_count'] * 8)),
                response_check=check, request_label=f'extraction format comparison {label}')
            check(wire)
            answer = normalized(wire)
        except (RuntimeError, ValueError) as exc:
            error = str(exc)
        unanchored = []
        for index, claim in enumerate((answer or {}).get('claims', [])):
            evidence = evidence_span_from_item(claim, case['target']['sentences'])
            if evidence is None or any(not source_surface_mention(evidence[2], claim[k]) for k in ('subject', 'object')):
                unanchored.append(index)
        result = {'number': case['number'], 'mode': mode, 'model': client.model,
            'thinking': client.settings.task_thinking['extraction'],
            'generation_budget': client.settings.extraction_min_output_budget,
            'valid_first_pass': answer is not None and error is None, 'error': error,
            'wire_answer': wire, 'answer': answer, 'unanchored_claim_indexes': unanchored,
            'elapsed_seconds': round(time.monotonic() - started, 3), 'requests': metrics}
        save_json(output / 'format_results' / (label + '.json'), result)
        emit(f'Finished {label}: {result["valid_first_pass"]}; {result["elapsed_seconds"]}s')
        return result

    # Reverse order on alternate targets to reduce a fixed first-call/cache bias.
    jobs = [(case, mode) for index, case in enumerate(cases)
            for mode in (('objects', 'rows') if index % 2 == 0 else ('rows', 'objects'))]
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(assess, jobs))
    signatures = {(r['model'], r['thinking'], r['generation_budget']) for r in results}
    if len(signatures) != 1:
        raise ValueError('Comparison settings differed')
    summary = {}
    for mode in ('objects', 'rows'):
        group = [r for r in results if r['mode'] == mode]
        usage = [m for r in group for m in r['requests'] if m.get('usage_reported')]
        summary[mode] = {'targets': len(group), 'valid_first_pass': sum(r['valid_first_pass'] for r in group),
            'claims': sum(len((r['answer'] or {}).get('claims', [])) for r in group),
            'unanchored_claims': sum(len(r['unanchored_claim_indexes']) for r in group),
            'median_seconds': round(median(r['elapsed_seconds'] for r in group), 3),
            **{k: sum(m.get(k) or 0 for m in usage) if usage else None
               for k in ('input_tokens', 'output_tokens', 'reasoning_tokens')}}
    differences = []
    for case in cases:
        pair = {r['mode']: r for r in results if r['number'] == case['number']}
        sets = {mode: {json.dumps(c, sort_keys=True, ensure_ascii=False) for c in
            (pair[mode]['answer'] or {}).get('claims', [])} for mode in pair}
        differences.append({'number': case['number'], 'identical_records': len(sets['objects'] & sets['rows']),
            'only_objects': [json.loads(c) for c in sorted(sets['objects'] - sets['rows'])],
            'only_rows': [json.loads(c) for c in sorted(sets['rows'] - sets['objects'])]})
    report = {'created_at': utc_now(), 'live_results_modified': False, 'recovery_disabled': True,
        'scientific_accuracy_evaluated': False, 'summary': summary, 'results': results, 'differences': differences}
    save_json(output / 'format_comparison.json', report)
    return report
