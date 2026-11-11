"""Paired first-pass wire-format checks, separate from scientific merge accuracy."""
import json
import time
from concurrent.futures import ThreadPoolExecutor
from statistics import median

from backend.shared.artifacts import save_json, save_rows
from backend.stages.ontology_grounding.contract import SYSTEM, packet_schema, check_response
from backend.stages.ontology_grounding.grounder import request_packet
from backend.stages.ontology_grounding import rows


def decision_fields(answer):
    values = {}
    for result in answer['results']:
        key = result['claim_id']
        values[(key, 'scope')] = result['scope']
        for role in ('subject', 'object', 'relation'):
            for field in ('mapping', 'term'):
                values[(key, role + '.' + field)] = result[role][field]
            if role != 'relation':
                values[(key, role + '.denotation')] = result[role + '_denotation']
    return values


def compare_formats(claims, reference, index, output, client_factory, *, batch_size=6, batches=3, workers=2):
    """Freeze identical inputs, disable recovery and retain both decisions.

    client_factory(format, metrics_callback, response_directory) supplies the
    same assigned model/Thinking for both modes. No active run is modified.
    """
    if not 1 <= batch_size <= 20 or not 1 <= batches <= 10 or not 1 <= workers <= 2:
        raise ValueError('Format comparison requires 1..20 claims, 1..10 batches and 1..2 workers')
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError('Benchmark output is not empty; choose a new directory')
    selected = [c for c in claims if not c.get('schema_error') and not c.get('superseded_by')][:batch_size * batches]
    if not selected:
        raise ValueError('No saved source claims are available for format comparison')
    output.mkdir(parents=True, exist_ok=True)
    save_rows(output / 'source_claims.jsonl', selected)
    packets = []
    for start in range(0, len(selected), batch_size):
        batch = selected[start:start + batch_size]
        payload, _ = request_packet(batch, index)
        packets.append({'number': len(packets) + 1, 'candidate_ids': [c['candidate_id'] for c in batch], 'payload': payload,
                        'json_system': SYSTEM, 'tsv_system': rows.instructions(payload), 'json_schema': packet_schema(payload)})
    save_rows(output / 'input_packets.jsonl', packets)

    def assess(item):
        packet, wire_format = item
        metrics = []
        label = f'{wire_format}-{packet["number"]}'
        client = client_factory(wire_format, metrics.append, output / 'responses' / label)
        if client.settings.json_regeneration_attempts or client.settings.json_repair_attempts:
            raise ValueError('Format benchmarking requires both repair and regeneration disabled')
        payload = packet['payload']
        user = json.dumps(payload, ensure_ascii=False)
        system = packet['tsv_system'] if wire_format == 'tsv' else packet['json_system']
        schema = packet['json_schema'] if wire_format == 'json' else None
        size = len(system) + len(user) + (len(json.dumps(schema)) if schema else 0)
        if size > client.settings.pathway_assembly_input_characters:
            raise ValueError('Comparison packet exceeds the input budget; use a smaller batch size')
        client.set_request_context({'stage': 'ontology_grounding', 'claim_count': len(payload['claims'])})
        started = time.monotonic()
        answer, error = None, None
        options = {'max_tokens': 12000, 'answer_token_hint': 100 + 400 * len(payload['claims']),
                   'request_label': f'grounding format benchmark {label}'}
        try:
            if wire_format == 'tsv':
                text = client.chat_text(system, user, response_check=lambda text: rows.parse_rows(text, payload), **options)
                answer = rows.parse_rows(text, payload)
            else:
                answer = client.chat_json(system, user, json_schema=schema,
                    response_check=lambda answer: check_response(answer, payload), **options)
            check_response(answer, payload)
        except (RuntimeError, ValueError) as exc:
            error = str(exc)
        result = {'packet': packet['number'], 'format': wire_format, 'candidate_ids': packet['candidate_ids'],
                  'model': client.model, 'thinking': client.thinking_enabled, 'valid_first_pass': answer is not None and error is None,
                  'error': error, 'elapsed_seconds': round(time.monotonic() - started, 3), 'answer': answer, 'requests': metrics}
        save_json(output / 'results' / (label + '.json'), result)
        return result

    # Alternate order within pairs; timings still depend on provider caching/load.
    work = [(p, f) for p in packets for f in (('json', 'tsv') if p['number'] % 2 else ('tsv', 'json'))]
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(assess, work))
    summary, differences = {}, []
    for wire_format in ('json', 'tsv'):
        mode = [r for r in results if r['format'] == wire_format]
        requests = [m for r in mode for m in r['requests']]
        usage = [m for m in requests if m.get('usage_reported')]
        summary[wire_format] = {'packets': len(mode), 'valid_first_pass': sum(r['valid_first_pass'] for r in mode),
            'requests': len(requests), 'recovery_requests': sum(m.get('purpose') != 'judgement' for m in requests),
            'median_seconds': round(median(r['elapsed_seconds'] for r in mode), 3),
            'usage_reported_requests': len(usage),
            'output_tokens': sum(m['output_tokens'] for m in usage) if usage else None,
            'input_tokens': sum(m['input_tokens'] for m in usage) if usage else None,
            'reasoning_tokens': sum(m['reasoning_tokens'] for m in usage) if usage else None}
    compared_fields = 0
    for packet in packets:
        pair = {r['format']: r for r in results if r['packet'] == packet['number']}
        if not all(r['valid_first_pass'] for r in pair.values()):
            continue
        left, right = (decision_fields(pair[f]['answer']) for f in ('json', 'tsv'))
        compared_fields += len(left)
        for (claim_id, field), value in left.items():
            if value != right[(claim_id, field)]:
                differences.append({'packet': packet['number'], 'claim_id': claim_id,
                    'candidate_id': packet['candidate_ids'][int(claim_id[1:]) - 1], 'field': field,
                    'json': value, 'tsv': right[(claim_id, field)]})
    report = {'catalog_signature': reference.signature, 'claims': len(selected), 'recovery_disabled': True,
        'scientific_accuracy_evaluated': False, 'summary': summary,
        'compared_decision_fields': compared_fields, 'differing_decision_fields': len(differences),
        'differences': differences, 'notes': [
            'Identical source claims, candidate aliases, definitions and qualifiers were used in both formats.',
            'Agreement is not scientific ground truth; disagreements require review.',
            'Small-sample latencies depend on provider load, cache state and concurrent requests.']}
    save_json(output / 'comparison.json', report)
    return report
