"""Small shared-paper requests, followed by an evidence-bound grouped synthesis."""
from copy import deepcopy
import json
from types import SimpleNamespace

from backend.providers.response_repair import validate_schema
from backend.providers.structured_output import IncompleteResponseError, EvidenceSelectionError
from backend.providers.llm import LLMServiceError
from backend.shared.cancellation import check_cancelled
from backend.stages.external_validation.joint import packed_document
from backend.stages.external_validation.paper_proofs import guard_paper, provenance
from backend.stages.external_validation.synthesis import (
    paper_contract, synthesis_instructions, _claim, _signature, _synthesis_schema,
    _synthesis_documents, judge_with_synthesis, POLICY,
)

TRUST_JUDGE_POLICY = 'shared-paper-trust-v1'


def paper_signature(candidate, document, model, thinking):
    return _signature({'policy': POLICY, 'claim': _claim(candidate), 'document': document,
                       'model': model, 'thinking': thinking})


def shared_documents(documents):
    """Merge actual passages, refusing conflicting paper/chunk identities."""
    merged = {}
    for document in documents:
        identifier = document['evidence_document_id']
        if identifier not in merged:
            merged[identifier] = {**deepcopy(document), 'evidence_chunks': []}
        target = merged[identifier]
        if document.get('doi') and target.get('doi') and document['doi'] != target['doi']:
            raise ValueError('Shared evidence paper has conflicting DOI identities')
        chunks = {c['chunk_id']: c for c in target['evidence_chunks']}
        for chunk in document.get('evidence_chunks') or []:
            previous = chunks.get(chunk['chunk_id'])
            if previous and previous['text'] != chunk['text']:
                raise ValueError('Shared evidence chunk ID names different source text')
            if not previous:
                target['evidence_chunks'].append(deepcopy(chunk)); chunks[chunk['chunk_id']] = chunk
    return merged


def _rows_schema(item, ids):
    item = deepcopy(item)
    item['properties'] = {'candidate_id': {'type': 'string', 'enum': ids}, **item['properties']}
    item['required'] = list(dict.fromkeys(['candidate_id', *item['required']]))
    return {'type': 'object', 'properties': {'results': {'type': 'array', 'items': item,
            'minItems': len(ids), 'maxItems': len(ids)}}, 'required': ['results'], 'additionalProperties': False}


def _identify(answer, ids):
    if sorted(r['candidate_id'] for r in answer['results']) != sorted(ids):
        raise IncompleteResponseError('Grouped judgement must identify every supplied claim exactly once')
    return {r['candidate_id']: r for r in answer['results']}


def _call(client, system, payload, schema, label, settings, check, budget):
    user = json.dumps(payload, ensure_ascii=False, separators=(',', ':'))
    if len(user) > settings.external_input_max_characters:
        raise LLMServiceError(f'{label}: grouped input exceeds the configured budget; no claim or paper was dropped')
    if hasattr(client, 'set_request_context'):
        client.set_request_context({'external_phase': label.split(':')[0],
            'claim_count': len(payload.get('claims', [])) or int('claim' in payload),
            'paper_count': len(payload.get('documents', []))})
    return client.chat_json(system, user, max_tokens=min(32768, max(budget, settings.external_min_output_budget)),
                            answer_token_hint=budget, json_schema=schema, request_label=label, response_check=check)


def judge_shared_paper(client, candidates, document, per_claim_documents, settings):
    ids = [c['candidate_id'] for c in candidates]
    if not ids or len(set(ids)) != len(ids):
        raise ValueError('Shared assessment requires one or more distinct claim identities')
    by_id = {c['candidate_id']: c for c in candidates}
    single = len(ids) == 1
    item, system = paper_contract(grouped=not single)
    if single:
        # There is exactly one possible claim/paper association. Do not ask the
        # model to copy that identity into an unnecessary collection envelope.
        schema = item
        system += (' Exactly ONE claim is supplied. Return ONLY the root decision and finding properties. '
                   'Do not return results, candidate_id, a claim-ID key, an array, or additional assessments.')
        payload = {'claim': _claim(candidates[0]), 'documents': [packed_document(document, candidates[0])]}
    else:
        # Identity belongs to the envelope, once. $defs avoids repeating the
        # full scientific contract for every claim in the JSON-mode prompt.
        schema = {'type': 'object', '$defs': {'paper_assessment': item}, 'properties': {
            'results': {'type': 'object', 'properties': {
                cid: {'$ref': '#/$defs/paper_assessment'} for cid in ids},
                'required': ids, 'additionalProperties': False}},
            'required': ['results'], 'additionalProperties': False}
        system += (f' Assess exactly {len(ids)} supplied claims independently against the shared ONE paper. '
                   'Return results as an OBJECT keyed by the supplied candidate IDs, not an array. '
                   'Write each candidate ID once as its result key. Each value contains ONLY decision and finding; '
                   'never add a candidate_id property inside a value or at the root. '
                   'Stop after one result per supplied claim; do not repeat an assessment. '
                   'Shared context does not allow one claim to establish another. Each row needs its own '
                   'endpoint/condition/quotation checks. Do not vote on whether the overall pathway sounds plausible.')
        payload = {'claims': [_claim(c) for c in candidates], 'documents': [packed_document(document, {})]}
    def check(answer):
        rows = {ids[0]: answer} if single else answer['results']
        if set(rows) != set(ids):
            raise IncompleteResponseError('Grouped judgement must identify every supplied claim exactly once')
        for cid, row in rows.items():
            if row['decision']['decision'] in {'supported', 'contradicted'}:
                _, errors = provenance(by_id[cid], per_claim_documents[cid], row['decision'])
                if errors:
                    raise EvidenceSelectionError('Correct paper-bound proof for ' + cid + ': ' + '; '.join(errors))
    answer = _call(client, system, payload, schema,
                 f"grouped paper external: {document['evidence_document_id']} / {len(ids)} claims",
                 settings, check, 2600 * len(ids))
    # A schema-valid wire object becomes the existing internal row format.
    # No malformed JSON, duplicate property or missing identity is corrected.
    rows = {ids[0]: answer} if single else answer['results']
    return {'results': [{'candidate_id': cid, **rows[cid]} for cid in ids]}


class _SynthesisReplay:
    """Run existing scientific post-checks with an already received grouped row."""
    def __init__(self, client, answer, settings, strict_proof=False):
        self.model, self.answer, self.settings = client.model, answer, settings
        self.strict_proof = strict_proof
        self.request_metrics = {'requests': 0}

    def for_model(self, model):
        return SimpleNamespace(model=model)

    def chat_json(self, system, user, **options):
        validate_schema(self.answer, options['json_schema'])
        try:
            options['response_check'](self.answer)
        except EvidenceSelectionError:
            if self.strict_proof:
                raise
        return deepcopy(self.answer)


def apply_synthesis(client, candidate, documents, records, answer, settings, *, strict_proof=False):
    return judge_with_synthesis(_SynthesisReplay(client, answer, settings, strict_proof), candidate, documents,
                                settings, saved_records=records)


def judge_group_synthesis(client, candidates, prepared, records, links, settings):
    ids = [c['candidate_id'] for c in candidates]
    all_documents, inputs, source_context_pool = [], [], {}
    for candidate in candidates:
        cid = candidate['candidate_id']
        docs = prepared[cid]['evidence_documents']
        decisions = [r['decision'] for r in records[cid]]
        packets, _ = _synthesis_documents(docs, decisions)
        all_documents.extend(packets)
        context = candidate.get('source_section_context') or candidate.get('source_local_context')
        context_id = _signature(context)
        source_context_pool.setdefault(context_id, context)
        inputs.append({'candidate_id': cid, 'claim': _claim(candidate), 'paper_findings': decisions,
                       'evidence_document_ids': [d['evidence_document_id'] for d in docs],
                       'source_scope_context_only': {'evidence': candidate.get('source_evidence_span'),
                                                     'local_context_ref': context_id}})
    # Merge only exact selected sentences; preserve all finding/context roles per claim
    # in its paper_findings. The per-claim guard still rejects unselected role evidence.
    combined = {}
    for packet in all_documents:
        target = combined.setdefault(packet['evidence_document_id'], {**packet, 'chunks': []})
        for chunk in packet['chunks']:
            previous = next((c for c in target['chunks'] if c['chunk_id'] == chunk['chunk_id']), None)
            if previous is None:
                target['chunks'].append(deepcopy(chunk)); continue
            rows = {r['number']: r for r in previous['sentences']}
            for row in chunk['sentences']:
                if row['number'] in rows and rows[row['number']]['text'] != row['text']:
                    raise ValueError('Grouped synthesis sentence identity conflicts')
                rows.setdefault(row['number'], deepcopy(row))
            previous['sentences'] = [rows[n] for n in sorted(rows)]
    single = _synthesis_schema(list(combined))
    # Each claim can have a different paper count; enforce its actual set in the response check.
    single['properties']['paper_dispositions'].pop('minItems')
    single['properties']['paper_dispositions'].pop('maxItems')
    schema = _rows_schema(single, ids)
    link_schema = {'type': 'object', 'properties': {
        'upstream_claim_id': {'type': 'string', 'enum': ids},
        'downstream_claim_id': {'type': 'string', 'enum': ids},
        'compatibility': {'type': 'string', 'enum': ['compatible', 'incompatible', 'unresolved']},
        'reason': {'type': 'string'}, 'evidence_document_ids': {'type': 'array', 'items': {'type': 'string', 'enum': list(combined)}}},
        'required': ['upstream_claim_id', 'downstream_claim_id', 'compatibility', 'reason', 'evidence_document_ids'],
        'additionalProperties': False}
    schema['properties']['pathway_connections'] = {'type': 'array', 'items': link_schema,
                                                   'minItems': len(links), 'maxItems': len(links)}
    schema['required'].append('pathway_connections')
    system = synthesis_instructions() + (
        'Any NLI diagnostics are model scores, not scientific evidence, and cannot override paper proof. '
        ' Assess the supplied claims INDEPENDENTLY and return one result with candidate_id per claim. '
        'Shared document text is deduplicated; each claim may use ONLY its own admitted findings and '
        'explicitly selected context. Role evidence from another claim cannot fill its gaps. '
        'source_scope_context_only.local_context_ref resolves to the exact text in source_context_pool. '
        'This pool is original-PDF context for scope and meaning ONLY, never external corroboration. '
        'Use one short sentence per rationale or paper-disposition reason. Do not restate the complete '
        'claim or quote in every explanation; preserve all scientific restrictions and exact proof anchors. '
        'Also assess every proposed pathway_connection exactly once, comparing entity identity, crops, '
        'conditions, doses and populations. Cite the supplied paper IDs supporting that compatibility '
        'assessment. Use unresolved for insufficient compatibility evidence. A compatible connection is '
        'not proof of causal transitivity or an experimentally demonstrated whole pathway. '
        'A failed edge must not discard other supported edges. Return pathway_connections=[] if none were supplied.')
    from backend.domain.ontology import ENTITY_DEFINITIONS, PATHWAY_DESCRIPTION
    payload = {'claims': inputs, 'documents': list(combined.values()), 'pathway_connections': links,
               'source_context_pool': source_context_pool,
               'ontology_definitions': ENTITY_DEFINITIONS, 'project_pathway': PATHWAY_DESCRIPTION}
    def check(answer):
        rows = _identify(answer, ids)
        expected = sorted((r['upstream_claim_id'], r['downstream_claim_id']) for r in links)
        actual = sorted((r['upstream_claim_id'], r['downstream_claim_id']) for r in answer['pathway_connections'])
        if actual != expected:
            raise IncompleteResponseError('Grouped synthesis must assess each actual graph connection exactly once')
        for index, candidate in enumerate(candidates):
            cid = candidate['candidate_id']
            row = {k: v for k, v in rows[cid].items() if k != 'candidate_id'}
            try:
                apply_synthesis(client, candidate, prepared[cid]['evidence_documents'], records[cid], row, settings, strict_proof=True)
            except EvidenceSelectionError as exc:
                # Only proof selections may change. Types, findings, dispositions,
                # other claims and connection judgements remain byte-for-byte data.
                exc.proof_repair = {
                    'path': ['results', next(i for i, r in enumerate(answer['results']) if r['candidate_id'] == cid), 'semantic_assessment', 'role_evidence'],
                    'schema': {'type': 'object', 'properties': {'role_evidence':
                        deepcopy(single['properties']['semantic_assessment']['properties']['role_evidence'])},
                        'required': ['role_evidence'], 'additionalProperties': False},
                    'input': {'claim': _claim(candidate), 'fixed_assessment': {k: v for k, v in row['semantic_assessment'].items() if k != 'role_evidence'},
                        'previous_role_evidence': row['semantic_assessment']['role_evidence'],
                        'error': str(exc), 'documents': _synthesis_documents(prepared[cid]['evidence_documents'],
                            [r['decision'] for r in records[cid]])[0]}}
                raise
        for connection in answer['pathway_connections']:
            allowed = set()
            for cid in (connection['upstream_claim_id'], connection['downstream_claim_id']):
                allowed.update(d['evidence_document_id'] for d in prepared[cid]['evidence_documents'])
            if not set(connection['evidence_document_ids']) <= allowed:
                raise IncompleteResponseError('Pathway compatibility cites a paper absent from its two claims')
            if connection['compatibility'] != 'unresolved' and not connection['evidence_document_ids']:
                raise IncompleteResponseError('A decisive pathway compatibility assessment needs cited evidence')
    return _call(client, system, payload, schema,
                  f'grouped cross-paper synthesis: {len(ids)} claims / {len(combined)} papers',
                  settings, check, 3000 * len(ids))


def bounded_groups(items, size):
    for start in range(0, len(items), size):
        check_cancelled()
        yield items[start:start + size]
