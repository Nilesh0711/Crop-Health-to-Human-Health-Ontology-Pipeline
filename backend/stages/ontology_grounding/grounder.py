"""Ground assertions in batches; retain occurrence identities and exact evidence."""
import json
from copy import deepcopy

from backend.shared.storage import stable_hash, utc_now, read_jsonl
from backend.shared.artifacts import save_rows
from backend.shared.cancellation import check_cancelled
from backend.stages.ontology_grounding.contract import SYSTEM, packet_schema, check_response
from backend.stages.ontology_grounding import rows as row_protocol
from backend.ontology.signatures import claim_signature
from backend.stages.ontology_grounding.scheduling import run_packets
from backend.stages.ontology_grounding.grouping import evidence_groups, group_signature, flag_consistency, GROUPING_POLICY
from backend.domain.extraction_review import extraction_warnings, refresh_eligibility, REVIEW_POLICY
from backend.domain.abbreviations import collect_definitions, relevant_definitions, expand_claim_names, VERSION as ABBREVIATIONS_VERSION

GROUNDING_POLICY = 'established-only-context-grounding-v6'
CLASS_SELECTION_POLICY = 'all-approved-classes-v1'


def catalog_term_map(reference):
    catalog = getattr(reference, 'catalog', reference)
    terms, term_iris = {}, {}
    for iri in catalog.profile['allowed_classes']:
        term = catalog.terms[iri]
        if not term['selectable'] or term['kind'] != 'class' or term['deprecated']:
            continue
        alias = f'T{len(terms) + 1}'
        entry = {'label': term['label']}
        if term.get('definition'):
            entry['definition'] = term['definition']
        if term.get('exact_synonyms'):
            entry['exact_synonyms'] = deepcopy(term['exact_synonyms'])
        if term.get('related_synonyms'):
            entry['related_synonyms'] = deepcopy(term['related_synonyms'])
        entry['parents'] = [catalog.terms.get(parent, {}).get('label', parent) for parent in term.get('parents', [])]
        terms[alias] = entry
        term_iris[alias] = iri
    relations, relation_iris = {}, {}
    for i, iri in enumerate(catalog.profile['allowed_object_properties'], 1):
        alias = f'R{i}'
        term = catalog.terms[iri]
        entry = {'label': term['label']}
        if term.get('definition'):
            entry['definition'] = term['definition']
        entry['domain'] = [catalog.terms.get(i, {}).get('label', i) for i in term.get('domain', [])]
        entry['range'] = [catalog.terms.get(i, {}).get('label', i) for i in term.get('range', [])]
        relations[alias] = entry
        relation_iris[alias] = iri
    return catalog, terms, term_iris, relations, relation_iris


def _bounded_reading_context(claim):
    reading = claim.get('source_reading_context') or claim.get('source_context_packet')
    if not reading:
        return None
    evidence_nums = claim.get('source_evidence_sentence_numbers') or []
    if evidence_nums and '\n' in reading:
        allowed = {n for num in evidence_nums for n in range(num - 3, num + 4)}
        lines = [line for line in reading.splitlines()
                 if line.startswith('[') and line[1:].split(']', 1)[0].isdigit()
                 and int(line[1:].split(']', 1)[0]) in allowed]
        if lines:
            return '\n'.join(lines)
    return reading


def request_packet(claims, reference, *, glossary=None):
    # Accept an existing offline index as well as a catalog; no retrieval is
    # performed. Every endpoint sees the same complete approved class list.
    catalog, terms, _, relations, _ = catalog_term_map(reference)
    glossary = collect_definitions(claims) if glossary is None else glossary
    proposals, reading_contexts = {}, {}
    for number, claim in enumerate(claims, 1):
        claim = expand_claim_names(claim, glossary)
        key = f'C{number}'
        item = {}
        for k in ('subject', 'subject_context', 'relation', 'object', 'object_context',
                  'qualifiers', 'source_evidence_span', 'source_local_context',
                  'extraction_type_hints', 'source_evidence_section_heading', 'source_section_context'):
            val = claim.get(k)
            if val not in (None, '', {}, []):
                item[k] = deepcopy(val)
        for role in ('subject', 'object'):
            mention = claim.get(f'{role}_source_mention')
            if mention and mention != item.get(role):
                item[f'{role}_source_mention'] = mention
        abbr = relevant_definitions(claim, glossary)
        if abbr:
            item['abbreviation_definitions'] = abbr
        reading = _bounded_reading_context(claim)
        if reading:
            context_id = 'context-' + stable_hash(str(claim.get('source_document_id', '')) + '|' + reading, 16)
            reading_contexts[context_id] = reading
            item['reading_context_id'] = context_id
        proposals[key] = item
    payload = {'scope': {k: catalog.profile[k] for k in ('id', 'crop', 'research_question', 'scope_description')},
               'terms': terms, 'relations': relations, 'reading_contexts': reading_contexts, 'claims': proposals}
    def check(answer):
        return check_response(answer, payload)
    return payload, check


def ground_claims(candidates, output, catalog, client, *, encoder=None, batch_size=3, workers=None, emit=lambda *args: None):
    if not 1 <= batch_size <= 3:
        raise ValueError('Ontology grounding batch size must be 1–3 claims')
    workers = workers if workers is not None else getattr(getattr(client, 'settings', None), 'ontology_grounding_workers', 1)
    if not 1 <= workers <= 8:
        raise ValueError('Grounding workers must be 1..8')
    model = getattr(client, 'model', 'configured-grounding-model')
    glossary = collect_definitions(candidates)
    thinking = getattr(getattr(client, 'settings', None), 'task_thinking', {}).get('pathway_assembly', False)
    wire_format = getattr(getattr(client, 'settings', None), 'ontology_grounding_output_format', 'json')
    if wire_format not in {'tsv', 'json'}:
        raise ValueError('Grounding output format must be tsv or json')
    # Hash the complete instruction template without making cache reuse depend
    # on which neighboring claims happen to share this request's batch.
    wire_system = row_protocol.instructions({'claims': {'C': {}}}) if wire_format == 'tsv' else SYSTEM
    config = stable_hash(json.dumps({'catalog': catalog.signature, 'model': model, 'thinking': thinking,
        'class_selection': CLASS_SELECTION_POLICY, 'policy': GROUNDING_POLICY,
        'grouping': GROUPING_POLICY, 'review_policy': REVIEW_POLICY, 'batch_size': batch_size,
        'input_budget': getattr(getattr(client, 'settings', None), 'pathway_assembly_input_characters', 140000),
        'prompt_sha256': stable_hash(wire_system, 64), 'output_format': wire_format,
        'abbreviations_version': ABBREVIATIONS_VERSION, 'abbreviation_definitions': glossary}, sort_keys=True), 64)
    path = output / 'ontology_grounding.jsonl'
    old = {r['candidate_id']: r for r in read_jsonl(path)}
    rows, pending = [], []
    valid = [c for c in candidates if not c.get('schema_error') and not c.get('superseded_by')]
    groups = evidence_groups(valid, batch_size)
    signatures = {c['candidate_id']: group_signature(group) for group in groups for c in group}
    for candidate in candidates:
        if candidate.get('schema_error') or candidate.get('superseded_by'):
            continue
        signature = claim_signature(candidate)
        previous = old.get(candidate['candidate_id'])
        if (previous and previous.get('claim_signature') == signature and previous.get('catalog_signature') == catalog.signature
                and previous.get('group_signature') == signatures[candidate['candidate_id']]
                and previous.get('config_signature') == config):
            rows.append(previous)
        else:
            pending.append(candidate)
    emit('info', f'Ontology grounding: {len(rows)} compatible results reused; {len(pending)} claims pending')
    # Preserve old signatures until each replacement is durably completed.
    persisted = dict(old)
    pending_ids = {c['candidate_id'] for c in pending}
    for group in groups:
        if any(c['candidate_id'] in pending_ids for c in group):
            pending_ids.update(c['candidate_id'] for c in group)
    rows = [r for r in rows if r['candidate_id'] not in pending_ids]
    batches = [[c for c in group if c['candidate_id'] in pending_ids] for group in groups]
    batches = [batch for batch in batches if batch]
    completed_rows = {r['candidate_id']: r for r in rows}

    def packets():
        scheduled, number = 0, 0
        while batches:
            check_cancelled()
            batch = batches.pop(0)
            payload, check = request_packet(batch, catalog, glossary=glossary)
            schema = packet_schema(payload) if wire_format == 'json' else None
            system = row_protocol.instructions(payload) if wire_format == 'tsv' else SYSTEM
            budget = getattr(getattr(client, 'settings', None), 'pathway_assembly_input_characters', 140000)
            if len(system) + len(json.dumps(payload, ensure_ascii=False)) + (len(json.dumps(schema)) if schema else 0) > budget:
                if len(batch) == 1:
                    raise ValueError('One grounding claim and its exact context exceed the input budget; evidence was not truncated')
                middle = len(batch) // 2
                batches[:0] = [batch[:middle], batch[middle:]]
                continue
            number += 1
            emit('stage', f'Ontology grounding: batch {number}; {len(batch)} evidence-linked claims')
            yield {'number': number, 'batch': batch, 'payload': payload, 'check': check, 'schema': schema, 'system': system,
                   'label': f'ontology grounding: {catalog.profile["id"]}; claims {scheduled + 1}-{scheduled + len(batch)}'}
            scheduled += len(batch)

    def assess(packet):
        batch, payload, check = packet['batch'], packet['payload'], packet['check']
        document_key = output.name
        if hasattr(client, 'set_request_context'):
            client.set_request_context({'stage': 'ontology_grounding', 'claim_count': len(batch)})
        user = json.dumps(payload, ensure_ascii=False)
        options = {'max_tokens': 12000, 'answer_token_hint': 100 + 400 * len(batch), 'request_label': packet['label']}
        if wire_format == 'tsv':
            text = client.chat_text(packet['system'], user,
                response_check=lambda text: row_protocol.parse_rows(text, payload), **options)
            answer = row_protocol.parse_rows(text, payload)
        else:
            answer = client.chat_json(packet['system'], user, json_schema=packet['schema'], response_check=check, **options)
        check(answer)
        selections = {row['claim_id']: row for row in answer['results']}
        records = []
        metrics = getattr(client, 'last_request_metrics', None) or {}
        actual_model = metrics.get('request_model') or metrics.get('model') or getattr(client, 'model', model)
        if actual_model != model:
            raise ValueError(f'Grounding response came from {actual_model}, but the configured primary is {model}; '
                             'the response was not saved as a primary-model judgement')
        _, _, term_iris, _, relation_iris = catalog_term_map(catalog)
        for n, candidate in enumerate(batch, 1):
            claim_key = f'C{n}'
            selection = selections[claim_key]
            bindings = {}
            for role in ('subject', 'object', 'relation'):
                result = deepcopy(selection[role])
                alias = result.pop('term')
                iri_map = relation_iris if role == 'relation' else term_iris
                iri = iri_map.get(alias) if alias else None
                concept = catalog.terms.get(iri, {}) if iri else {}
                result.update(iri=iri, label=concept.get('label'))
                result['canonical_iri'] = catalog.canonical_equivalent(iri) if iri else None
                if role != 'relation':
                    # Normalize the validated wire shape into the existing stored
                    # binding; the classification remains the model's decision.
                    result['denotation'] = selection[role + '_denotation']
                    occurrence = 'mention-' + stable_hash(f'{document_key}|{candidate["candidate_id"]}|{role}', 32)
                    result['occurrence_id'] = occurrence
                    result['entity_id'] = occurrence  # Reviewed sharing is centralized below.
                    candidate_aliases = payload['claims'][claim_key].get(role + '_candidates', list(payload['terms']))
                    result['candidate_iris'] = [term_iris[a] for a in candidate_aliases if a in term_iris]
                bindings[role] = result
            record = {'candidate_id': candidate['candidate_id'], 'document_key': document_key,
                'claim_signature': claim_signature(candidate), 'config_signature': config,
                'group_signature': signatures[candidate['candidate_id']], 'grouping_policy': GROUPING_POLICY,
                'group_size_limit': batch_size,
                'input_signature': stable_hash(user, 64),
                'extraction_warnings': extraction_warnings(candidate),
                'catalog_signature': catalog.signature, 'scope_id': catalog.profile['id'],
                'created_at': utc_now(), 'scope': selection['scope'], 'scope_reason': selection['scope_reason'],
                'abbreviation_definitions': payload['claims'][claim_key].get('abbreviation_definitions', []),
                'bindings': bindings, 'original_claim': deepcopy(candidate),
                'mapping_review': 'model_proposed', 'scientific_validation': 'not_assessed',
                'policy': GROUNDING_POLICY, 'model': actual_model, 'thinking': thinking,
                'class_selection_policy': CLASS_SELECTION_POLICY,
                'model_provenance': 'request_metrics' if metrics else 'configured_client',
                'provider_request_id': metrics.get('provider_request_id'),
                'response_artifact': metrics.get('response_artifact'),
                'response_format': metrics.get('response_format'),
                'output_protocol': row_protocol.VERSION if wire_format == 'tsv' else 'grounding-json-v1',
                'resolved_model': metrics.get('resolved_model')}
            previous = old.get(candidate['candidate_id'])
            if previous and previous.get('claim_signature') == record['claim_signature']:
                if previous.get('extraction_review'):
                    record['extraction_review'] = deepcopy(previous['extraction_review'])
                    record['review_history'] = deepcopy(previous.get('review_history', []))
            refresh_eligibility(record)
            records.append(record)
        return flag_consistency(records)

    def publish(records):
        for record in records:
            completed_rows[record['candidate_id']] = record
            persisted[record['candidate_id']] = record
        save_rows(path, list(persisted.values()))

    run_packets(packets(), assess, publish, workers)
    rows = [completed_rows[c['candidate_id']] for c in candidates if c['candidate_id'] in completed_rows]
    valid_ids = {c['candidate_id'] for c in candidates if not c.get('schema_error') and not c.get('superseded_by')}
    rows = [r for r in rows if r['candidate_id'] in valid_ids]
    rows = flag_consistency(rows)
    save_rows(path, rows)
    return rows
