"""Ground assertions in batches; retain occurrence identities and exact evidence."""
import json
from copy import deepcopy

from backend.providers.llm import LLMResponseError
from backend.providers.structured_output import StructuredOutputError
from backend.shared.storage import stable_hash, utc_now, read_jsonl, file_hash
from backend.shared.artifacts import save_rows
from backend.shared.cancellation import check_cancelled
from backend.stages.ontology_grounding.contract import ENTITY_SYSTEM, SYSTEM, packet_schema, check_response
from backend.stages.ontology_grounding import rows as row_protocol
from backend.ontology.signatures import claim_signature
from backend.ontology.policies import ENTITY_GROUNDING_POLICY, GROUNDING_POLICY
from backend.ontology.inventory_policy import POLICY as INVENTORY_POLICY
from backend.stages.ontology_grounding.scheduling import run_packets
from backend.stages.ontology_grounding.grouping import (
    CONSISTENCY_POLICY, GROUPING_POLICY, PREVIOUS_CONSISTENCY_POLICY, evidence_groups, flag_consistency, group_signature,
)
from backend.domain.extraction_review import extraction_warnings, refresh_eligibility, REVIEW_POLICY, ELIGIBILITY_POLICY
from backend.domain.classification import CLASSIFICATION_POLICY
from backend.ontology.usage import ANNOTATION_POLICY, USAGE_POLICY, ANNOTATION_PROJECTION_POLICY
from backend.ontology.property_types import (
    PROPERTY_TYPE_POLICY,
    VERIFIABLE_UPPER_IRIS,
    check_property_types,
    class_ancestors,
    effective_property_restrictions,
)
from backend.stages.claim_extraction.contract import (
    CLOSED_PREDICATE_SET_VERSION,
    closed_predicate_whitelist_digest,
    uses_entity_only_grounding,
)
from backend.stages.ontology_grounding.vocabulary import catalog_term_map
from backend.stages.ontology_grounding.shortlists import cultivar_definitions, lexical_properties, treatment_candidates
from backend.domain.abbreviations import collect_definitions, relevant_definitions, expand_claim_names, VERSION as ABBREVIATIONS_VERSION
from backend.stages.ontology_grounding.source_context import definition_contexts, POLICY as DEFINITION_CONTEXT_POLICY

CLASS_SELECTION_POLICY = 'all-approved-classes-v1'
OBSERVATION_NOTE = ('\nFor a record with measured_comparison, ground the reported group or condition and measured trait '
                    'independently. The record has no fixed property. Preserve the source comparison and its uncertainty.')


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


def _fixed_property_entry(catalog, relation_meta, claim):
    fixed_iri = claim['proposed_relation_iri']
    term = catalog.terms.get(fixed_iri, {})
    entry = {
        'alias': claim.get('predicate_alias'),
        'iri': fixed_iri,
        'id': claim.get('proposed_relation_id') or fixed_iri.rsplit('/', 1)[-1],
        'label': relation_meta.get('label') or term.get('label'),
    }
    for field in ('definition', 'usage_notes', 'comments', 'domain', 'range', 'parent_properties', 'inverse_of'):
        if relation_meta.get(field):
            entry[field] = deepcopy(relation_meta[field])
    entry['proposed_mapping'] = claim.get('proposed_relation_mapping', 'exact')
    if claim.get('proposed_relation_detail_loss'):
        entry['proposed_detail_loss'] = list(claim['proposed_relation_detail_loss'])
    return entry


def _compatible_aliases(choices, lineages_by_alias, required_iris, unsupported):
    if unsupported:
        return []
    req = set(required_iris or ())
    if not req:
        return list(choices)
    return [alias for alias in choices if req <= lineages_by_alias[alias]]


def _closed_relation_issue_no_inventory(catalog, candidate, bindings, iri, mapping):
    if not (bindings['subject'].get('iri') and bindings['object'].get('iri')):
        return 'Binary fixed property requires resolved subject and object endpoints.'
    if candidate.get('passive_role_reversal_verified') is False:
        return 'Passive source wording was not reversed into forward subject -> object order.'
    reqs = effective_property_restrictions(catalog, iri)
    requires_types = bool(reqs['domain'] or reqs['range'] or reqs['unsupported_domain'] or reqs['unsupported_range'])
    status = check_property_types({'bindings': {**bindings, 'relation': {'iri': iri, 'mapping': mapping}}}, catalog)['status']
    if status != 'compatible' and not (status == 'not_applicable' and not requires_types):
        return f'The proposed property requires verified compatible endpoint types ({status}).'
    return None


def request_packet(claims, reference, *, glossary=None, source_definitions=None, inventory=None, cultivars=None):
    catalog = getattr(reference, 'catalog', reference)
    entity_only = bool(claims) and all(uses_entity_only_grounding(claim) for claim in claims)
    fixed_iris = {claim['proposed_relation_iri'] for claim in claims if claim.get('proposed_relation_iri')} if entity_only else set()
    selected_props = fixed_iris if entity_only else (set() if inventory else None)
    catalog, terms, core_iris, relations, relation_iris = catalog_term_map(
        reference, selected_properties=selected_props, include_classes=inventory is None)
    core_by_iri = {iri: terms[alias] for alias, iri in core_iris.items()}
    fixed_meta_by_iri = {relation_iris[alias]: meta for alias, meta in relations.items()} if entity_only else {}
    selected_aliases = {}
    shortlisted_properties = set()
    aliases_by_property = {iri: f'R{i}' for i, iri in enumerate(catalog.profile['allowed_object_properties'], 1)}
    if inventory is not None:
        terms = {}
    glossary = collect_definitions(claims) if glossary is None else glossary
    proposals, reading_contexts, definition_records = {}, {}, {}
    for number, claim in enumerate(claims, 1):
        claim = expand_claim_names(claim, glossary)
        key = f'C{number}'
        item = {}
        for k in ('subject', 'subject_context', 'relation', 'object', 'object_context',
                  'qualifiers', 'measured_comparison', 'source_evidence_span', 'source_local_context',
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
        for role, records in (source_definitions or {}).get(claim['candidate_id'], {}).items():
            ids = []
            for record in records:
                context_id = 'definition-' + stable_hash(json.dumps(record, sort_keys=True), 24)
                definition_records[context_id] = deepcopy(record)
                ids.append(context_id)
            if ids:
                item[role + '_definition_context_ids'] = ids
        reading = _bounded_reading_context(claim)
        if reading:
            context_id = 'context-' + stable_hash(str(claim.get('source_document_id', '')) + '|' + reading, 16)
            reading_contexts[context_id] = reading
            item['reading_context_id'] = context_id
        fixed_iri = claim.get('proposed_relation_iri') if entity_only else None
        fixed_reqs = effective_property_restrictions(catalog, fixed_iri) if fixed_iri else None
        if fixed_iri:
            item['fixed_property'] = _fixed_property_entry(catalog, fixed_meta_by_iri.get(fixed_iri, {}), claim)
        if inventory is not None:
            lineages = {}
            for role in ('subject', 'object'):
                cultivar = (cultivars or {}).get((claim['candidate_id'], role))
                if cultivar:
                    item[role + '_cultivar_source'] = cultivar
                treatment_iris, setup = treatment_candidates(catalog, claim, role, glossary, inventory=inventory)
                if setup:
                    item[role + '_treatment_source'] = setup
                methods = ' '.join(definition_records[k]['text'] for k in item.get(role + '_definition_context_ids', []))
                req_ancestors = fixed_reqs['domain' if role == 'subject' else 'range'] if fixed_reqs else ()
                hits = inventory.candidates(catalog, item[role], (item.get('extraction_type_hints') or {}).get(role, ''),
                                            item.get('source_evidence_span', ''), abbr, cultivar=cultivar,
                                            treatment_iris=treatment_iris, treatment_source=setup, methods_context=methods,
                                            required_ancestors=req_ancestors)
                choices = []
                lineages[role] = {}
                for term in hits:
                    iri = term['iri']
                    ancestors = set(inventory.lineage(catalog, iri))
                    upper_types = [catalog.terms[upper]['label'] for upper in VERIFIABLE_UPPER_IRIS
                                   if upper in ancestors and upper in catalog.terms]
                    if iri not in selected_aliases:
                        alias = f'T{len(terms) + 1}'
                        selected_aliases[iri] = alias
                        terms[alias] = {**core_by_iri.get(iri, {}), **term}
                        if upper_types:
                            terms[alias]['upper_types'] = upper_types
                    alias = selected_aliases[iri]
                    choices.append(alias)
                    lineages[role][alias] = ancestors
                item[role + '_candidates'] = choices
            if fixed_reqs:
                dom_matches = _compatible_aliases(item['subject_candidates'], lineages['subject'],
                                                  fixed_reqs['domain'], fixed_reqs['unsupported_domain'])
                rng_matches = _compatible_aliases(item['object_candidates'], lineages['object'],
                                                  fixed_reqs['range'], fixed_reqs['unsupported_range'])
                item['domain_compatible_subject_candidates'] = dom_matches
                item['range_compatible_object_candidates'] = rng_matches
                item['verified_relation_endpoint_matches'] = {'subject': dom_matches, 'object': rng_matches}
            elif not entity_only:
                candidates = ([] if item.get('measured_comparison') else lexical_properties(catalog, item['relation'], lineages))
                shortlisted_properties.update(iri for iri, _ in candidates)
                item['relation_candidates'] = [aliases_by_property[iri] for iri, _ in candidates]
                if type_matches := {aliases_by_property[iri]: roles for iri, roles in candidates if roles}:
                    item['verified_relation_endpoint_matches'] = type_matches
        elif fixed_reqs:
            all_aliases = list(terms)
            static_lineages = {alias: set(class_ancestors(catalog, core_iris[alias])) for alias in all_aliases}
            dom_matches = _compatible_aliases(all_aliases, static_lineages, fixed_reqs['domain'], fixed_reqs['unsupported_domain'])
            rng_matches = _compatible_aliases(all_aliases, static_lineages, fixed_reqs['range'], fixed_reqs['unsupported_range'])
            item['domain_compatible_subject_candidates'] = dom_matches
            item['range_compatible_object_candidates'] = rng_matches
            item['verified_relation_endpoint_matches'] = {'subject': dom_matches, 'object': rng_matches}
        proposals[key] = item
    if inventory and not entity_only:
        _, _, _, relations, _ = catalog_term_map(reference, selected_properties=shortlisted_properties)
    payload = {'scope': {k: catalog.profile[k] for k in ('id', 'crop', 'research_question', 'scope_description')},
               'terms': terms, 'relations': relations, 'reading_contexts': reading_contexts, 'claims': proposals}
    if entity_only:
        payload['entity_only'] = True
    if definition_records:
        payload['definition_contexts'] = definition_records
    def check(answer):
        return check_response(answer, payload)
    return payload, check


def ground_claims(candidates, output, catalog, client, *, encoder=None, batch_size=1, workers=None,
                  emit=lambda *args: None, source_packets=None, include_definitions=True, inventory=None,
                  preserve_unselected=False):
    if not 1 <= batch_size <= 3:
        raise ValueError('Ontology grounding batch size must be 1–3 claims')
    workers = workers if workers is not None else getattr(getattr(client, 'settings', None), 'ontology_grounding_workers', 1)
    if not 1 <= workers <= 8:
        raise ValueError('Grounding workers must be 1..8')
    selection_policy = INVENTORY_POLICY if inventory else CLASS_SELECTION_POLICY
    model = getattr(client, 'model', 'configured-grounding-model')
    glossary = collect_definitions(candidates)
    cultivars = cultivar_definitions(candidates, catalog, inventory=inventory) if inventory else {}
    source_file_hash = (file_hash(output / 'extraction_contexts.jsonl')
                        if include_definitions and source_packets is None else None)
    packets_source = (read_jsonl(output / 'extraction_contexts.jsonl')
                      if source_packets is None else source_packets)
    source_definitions = definition_contexts(candidates, packets_source, glossary) if include_definitions else {}
    definitions_signature = stable_hash(json.dumps(source_definitions, sort_keys=True, ensure_ascii=False), 64)
    thinking = getattr(getattr(client, 'settings', None), 'task_thinking', {}).get('pathway_assembly', False)
    wire_format = getattr(getattr(client, 'settings', None), 'ontology_grounding_output_format', 'json')
    if wire_format not in {'tsv', 'json'}:
        raise ValueError('Grounding output format must be tsv or json')
    valid = [c for c in candidates if not c.get('schema_error') and not c.get('superseded_by')]
    closed_flags = {uses_entity_only_grounding(c) for c in valid}
    if len(closed_flags) > 1:
        raise ValueError('Cannot mix closed-relation claims and open-relation claims in one grounding run')
    entity_only_run = bool(valid) and next(iter(closed_flags))
    grounding_policy = ENTITY_GROUNDING_POLICY if entity_only_run else GROUNDING_POLICY
    # Hash the complete instruction template without making cache reuse depend
    # on which neighboring claims happen to share this request's batch.
    if entity_only_run:
        wire_system = (row_protocol.instructions({'entity_only': True, 'claims': {'C': {}}})
                       if wire_format == 'tsv' else ENTITY_SYSTEM)
    else:
        wire_system = row_protocol.instructions({'claims': {'C': {}}}) if wire_format == 'tsv' else SYSTEM
    config_payload = {'catalog': catalog.signature, 'model': model, 'thinking': thinking,
        'inventory_signature': inventory.signature if inventory else None, 'class_selection': selection_policy, 'policy': grounding_policy,
        'grouping': GROUPING_POLICY, 'consistency': CONSISTENCY_POLICY,
        'review_policy': REVIEW_POLICY, 'eligibility_policy': ELIGIBILITY_POLICY,
        'classification_policy': CLASSIFICATION_POLICY,
        'property_type_policy': PROPERTY_TYPE_POLICY,
        'annotation_policy': ANNOTATION_POLICY, 'property_usage_policy': USAGE_POLICY,
        'annotation_projection_policy': ANNOTATION_PROJECTION_POLICY,
        'batch_size': batch_size,
        'input_budget': getattr(getattr(client, 'settings', None), 'pathway_assembly_input_characters', 140000),
        'prompt_sha256': stable_hash(wire_system, 64), 'output_format': wire_format,
        'definition_context_policy': DEFINITION_CONTEXT_POLICY if include_definitions else 'disabled',
        'definition_context_signature': definitions_signature, 'definition_file_sha256': source_file_hash,
        'cultivar_sources': sorted(cultivars.items()), 'abbreviations_version': ABBREVIATIONS_VERSION,
        'abbreviation_definitions': glossary}
    if entity_only_run:
        config_payload.update({
            'predicate_set_version': CLOSED_PREDICATE_SET_VERSION,
            'predicate_whitelist_sha256': closed_predicate_whitelist_digest(),
        })
        if any(c.get('measured_comparison') for c in valid):
            config_payload['measured_observation_instructions'] = stable_hash(OBSERVATION_NOTE, 64)
    config = stable_hash(json.dumps(config_payload, sort_keys=True), 64)
    # This policy change only reassesses completed entity decisions. The
    # original model input and every other compatibility check must still match.
    previous_config = stable_hash(json.dumps(
        {**config_payload, 'consistency': PREVIOUS_CONSISTENCY_POLICY}, sort_keys=True), 64) if entity_only_run else None
    path = output / 'ontology_grounding.jsonl'
    old = {r['candidate_id']: r for r in read_jsonl(path)}
    rows, pending = [], []
    groups = evidence_groups(valid, batch_size)
    signatures = {c['candidate_id']: group_signature(group) for group in groups for c in group}
    for candidate in candidates:
        if candidate.get('schema_error') or candidate.get('superseded_by'):
            continue
        signature = claim_signature(candidate)
        previous = old.get(candidate['candidate_id'])
        if (previous and previous.get('claim_signature') == signature and previous.get('catalog_signature') == catalog.signature
                and previous.get('group_signature') == signatures[candidate['candidate_id']]
                and (previous.get('config_signature') == config
                     or (previous_config and previous.get('config_signature') == previous_config
                         and previous.get('consistency_policy') == PREVIOUS_CONSISTENCY_POLICY))):
            rows.append({**previous, 'config_signature': config})
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

    def prepare_packet(batch, number, label):
        payload, check = request_packet(batch, catalog, glossary=glossary, source_definitions=source_definitions,
                                        inventory=inventory, cultivars=cultivars)
        schema = packet_schema(payload) if wire_format == 'json' else None
        system = (row_protocol.instructions(payload) if wire_format == 'tsv'
                  else (ENTITY_SYSTEM if payload.get('entity_only') else SYSTEM))
        if payload.get('entity_only') and any(c.get('measured_comparison') for c in batch):
            system += OBSERVATION_NOTE
        user = json.dumps(payload, ensure_ascii=False)
        budget = getattr(getattr(client, 'settings', None), 'pathway_assembly_input_characters', 140000)
        if len(system) + len(user) + (len(json.dumps(schema)) if schema else 0) > budget:
            if len(batch) == 1:
                raise ValueError('One grounding claim and its exact context exceed the input budget; evidence was not truncated')
            return None
        return {'number': number, 'batch': batch, 'payload': payload, 'check': check,
                'schema': schema, 'system': system, 'user': user, 'label': label}

    def packets():
        scheduled, number = 0, 0
        while batches:
            check_cancelled()
            batch = batches.pop(0)
            label = f'ontology grounding: {catalog.profile["id"]}; claims {scheduled + 1}-{scheduled + len(batch)}'
            packet = prepare_packet(batch, number + 1, label)
            if packet is None:
                middle = len(batch) // 2
                batches[:0] = [batch[:middle], batch[middle:]]
                continue
            number += 1
            emit('stage', f'Ontology grounding: batch {number}; {len(batch)} evidence-linked claims')
            yield packet
            scheduled += len(batch)

    def recover(packet, error):
        batch = packet['batch']
        if len(batch) == 1 or not isinstance(error, (LLMResponseError, StructuredOutputError)):
            return ()
        emit('warning', f'Ontology grounding: batch {packet["number"]} returned an invalid response. '
             f'Retrying its {len(batch)} claims individually with their own candidate lists.')
        return [prepare_packet([candidate], packet['number'],
                               f'{packet["label"]}; individual retry {i}/{len(batch)}')
                for i, candidate in enumerate(batch, 1)]

    def assess(packet):
        batch, payload, check = packet['batch'], packet['payload'], packet['check']
        document_key = output.name
        if hasattr(client, 'set_request_context'):
            client.set_request_context({'stage': 'ontology_grounding', 'claim_count': len(batch),
                                        'split_on_invalid_response': len(batch) > 1})
        user = packet['user']
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
        relation_iris = {f'R{i}': iri for i, iri in enumerate(catalog.profile['allowed_object_properties'], 1)}
        if inventory:
            term_iris = {alias: term['iri'] for alias, term in payload['terms'].items()}
        else:
            _, _, term_iris, _, _ = catalog_term_map(catalog)
        entity_packet = bool(payload.get('entity_only'))
        for n, candidate in enumerate(batch, 1):
            claim_key = f'C{n}'
            selection = selections[claim_key]
            bindings = {}
            roles = ('subject', 'object') if entity_packet else ('subject', 'object', 'relation')
            for role in roles:
                result = deepcopy(selection[role])
                alias = result.pop('term')
                iri_map = relation_iris if role == 'relation' else term_iris
                iri = iri_map.get(alias) if alias else None
                concept = catalog.terms.get(iri, {}) if iri else {}
                if inventory and role != 'relation' and alias:
                    concept = payload['terms'][alias]
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
                    if inventory:
                        result['candidate_terms'] = [payload['terms'][a] for a in candidate_aliases]
                        if source := payload['claims'][claim_key].get(role + '_cultivar_source'):
                            result['cultivar_source'] = source
                        if source := payload['claims'][claim_key].get(role + '_treatment_source'):
                            result['treatment_source'] = source
                        if iri:
                            context_ids = payload['claims'][claim_key].get(role + '_definition_context_ids', [])
                            context = ' '.join(str(payload.get('definition_contexts', {}).get(k, {}).get('text', '')) for k in context_ids)
                            proposed = {key: result[key] for key in ('mapping', 'iri', 'label', 'reason')}
                            inventory.admit(payload['claims'][claim_key], role, result, concept, context)
                            if proposed['mapping'] != result['mapping']:
                                result['model_proposal'] = proposed
                            if result.get('iri'):
                                result['retrieved_term'] = concept
                                result['verified_ancestor_iris'] = inventory.ancestors(catalog, iri)
                if inventory and role == 'relation':
                    relation_candidates = payload['claims'][claim_key]['relation_candidates']
                    result['candidate_iris'] = [relation_iris[a] for a in relation_candidates]
                    result['candidate_terms'] = [
                        {'iri': relation_iris[a], 'label': payload['relations'][a]['label'],
                         'definition': payload['relations'][a].get('definition', '')}
                        for a in relation_candidates
                    ]
                if inventory and role == 'relation' and iri:
                    issue = inventory.relation_issue(catalog, candidate, bindings, iri, result['mapping'])
                    if issue:
                        result['model_proposal'] = {key: result[key] for key in ('mapping', 'iri', 'label', 'reason')}
                        result.update(mapping='unresolved', iri=None, label=None, canonical_iri=None, admission_reason=issue)
                bindings[role] = result
            if entity_packet and candidate.get('measured_comparison'):
                bindings['relation'] = {
                    'iri': None, 'label': None, 'canonical_iri': None, 'mapping': 'unresolved',
                    'reason': 'Measured comparison retained as an observation.',
                    'fixed_from_extraction': True,
                }
            elif entity_packet:
                fixed_iri = candidate['proposed_relation_iri']
                fixed_term = catalog.terms.get(fixed_iri, {})
                allowed = fixed_term.get('allowed_mappings', ('exact', 'broader', 'unresolved'))
                fixed_mapping = candidate.get('proposed_relation_mapping') or 'broader'
                if fixed_mapping not in allowed:
                    fixed_mapping = 'broader' if 'broader' in allowed else 'unresolved'
                detail_loss = list(candidate.get('proposed_relation_detail_loss') or [])
                rel_reason = (
                    f"Fixed during closed-relation extraction ({candidate.get('predicate_alias') or ''}): "
                    f"{fixed_term.get('label') or fixed_iri}"
                    + (f" [omits: {', '.join(detail_loss)}]" if detail_loss else '.')
                )
                rel_binding = {
                    'iri': fixed_iri,
                    'label': fixed_term.get('label'),
                    'canonical_iri': catalog.canonical_equivalent(fixed_iri),
                    'mapping': fixed_mapping,
                    'reason': rel_reason,
                    'fixed_from_extraction': True,
                    'predicate_alias': candidate.get('predicate_alias'),
                    'predicate_set_version': candidate.get('predicate_set_version', CLOSED_PREDICATE_SET_VERSION),
                    'candidate_iris': [fixed_iri],
                    'candidate_terms': [{
                        'iri': fixed_iri,
                        'label': fixed_term.get('label'),
                        'definition': fixed_term.get('definition', ''),
                    }],
                }
                if fixed_mapping == 'unresolved':
                    issue = f"Fixed property '{fixed_term.get('label') or fixed_iri}' permits only {list(allowed)}."
                elif inventory is not None:
                    issue = inventory.relation_issue(catalog, candidate, bindings, fixed_iri, fixed_mapping)
                else:
                    issue = _closed_relation_issue_no_inventory(catalog, candidate, bindings, fixed_iri, fixed_mapping)
                if issue:
                    rel_binding['under_review'] = True
                    rel_binding['admission_reason'] = issue
                bindings['relation'] = rel_binding
            output_protocol = (
                row_protocol.ENTITY_VERSION if (wire_format == 'tsv' and entity_packet)
                else (row_protocol.VERSION if wire_format == 'tsv'
                      else ('entity-grounding-json-v1' if entity_packet else 'grounding-json-v1'))
            )
            record = {'candidate_id': candidate['candidate_id'], 'document_key': document_key,
                'claim_signature': claim_signature(candidate), 'config_signature': config,
                'group_signature': signatures[candidate['candidate_id']], 'grouping_policy': GROUPING_POLICY,
                'group_size_limit': batch_size,
                'input_signature': stable_hash(user, 64),
                'extraction_warnings': extraction_warnings(candidate),
                'catalog_signature': catalog.signature, 'scope_id': catalog.profile['id'],
                'created_at': utc_now(), 'scope': selection['scope'], 'scope_reason': selection['scope_reason'],
                'abbreviation_definitions': payload['claims'][claim_key].get('abbreviation_definitions', []),
                'source_definition_context': deepcopy(source_definitions.get(candidate['candidate_id'], {})),
                'definition_context_policy': DEFINITION_CONTEXT_POLICY if include_definitions else 'disabled',
                'definition_context_signature': definitions_signature,
                'definition_file_sha256': source_file_hash,
                'bindings': bindings, 'original_claim': deepcopy(candidate),
                'mapping_review': 'model_proposed', 'scientific_validation': 'not_assessed',
                'policy': grounding_policy, 'model': actual_model, 'thinking': thinking,
                'classification_policy': CLASSIFICATION_POLICY,
                'annotation_policy': ANNOTATION_POLICY, 'property_usage_policy': USAGE_POLICY,
                'annotation_projection_policy': ANNOTATION_PROJECTION_POLICY,
                'property_type_policy': PROPERTY_TYPE_POLICY,
                'class_selection_policy': selection_policy, 'inventory_signature': inventory.signature if inventory else None,
                'model_provenance': 'request_metrics' if metrics else 'configured_client',
                'provider_request_id': metrics.get('provider_request_id'),
                'response_artifact': metrics.get('response_artifact'),
                'response_format': metrics.get('response_format'),
                'output_protocol': output_protocol,
                'resolved_model': metrics.get('resolved_model')}
            for role in ('subject', 'object'):
                components = bindings[role].get('components') or {}
                if components.get('status') == 'held_bundled_organs':
                    record['extraction_warnings'].append({'code': 'possible_bundled_outcomes', 'role': role,
                        'message': components['summary'], 'source_text': candidate.get('source_evidence_span', '')})
            if candidate.get('measured_comparison'):
                from backend.domain.measured_comparison import assess_observation
                assess_observation(record, inventory)
            previous = old.get(candidate['candidate_id'])
            if previous and previous.get('claim_signature') == record['claim_signature']:
                if previous.get('extraction_review'):
                    record['extraction_review'] = deepcopy(previous['extraction_review'])
                    record['review_history'] = deepcopy(previous.get('review_history', []))
            refresh_eligibility(record, catalog)
            records.append(record)
        return flag_consistency(records)

    def publish(records):
        for record in records:
            completed_rows[record['candidate_id']] = record
        # Only current completed inputs participate. Old pending rows remain
        # archived on disk without contaminating the current comparison.
        for record in flag_consistency(list(completed_rows.values())):
            persisted[record['candidate_id']] = record
        save_rows(path, list(persisted.values()))

    run_packets(packets(), assess, publish, workers, recover=recover)
    rows = [completed_rows[c['candidate_id']] for c in candidates if c['candidate_id'] in completed_rows]
    valid_ids = {c['candidate_id'] for c in candidates if not c.get('schema_error') and not c.get('superseded_by')}
    rows = [r for r in rows if r['candidate_id'] in valid_ids]
    rows = flag_consistency(rows)
    if preserve_unselected:
        persisted.update({row['candidate_id']: row for row in rows})
    save_rows(path, list(persisted.values()) if preserve_unselected else rows)
    return rows
