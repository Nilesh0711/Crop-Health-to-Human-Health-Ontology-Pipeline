"""MiMo Flash plans only the neighboring claims and their local junction."""
import json
from backend.stages.neighbor_validation.targets import search_targets
from backend.providers.structured_output import IncompleteResponseError


def translate_pathway(client, target, retained_queries=None):
    facets = search_targets(target)
    retained = retained_queries or {}
    missing = [r for r in facets if r['target_id'] not in retained]
    ids = [r['target_id'] for r in missing]
    if not ids:
        return {'queries': [retained[r['target_id']] for r in facets]}
    item = {'type': 'object', 'properties': {
        'target_id': {'type': 'string', 'enum': ids},
        'query': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
        'openalex_query': {'type': 'string', 'minLength': 1, 'maxLength': 240}},
        'required': ['target_id', 'query', 'openalex_query'], 'additionalProperties': False}
    schema = {'type': 'object', 'properties': {'queries': {'type': 'array', 'items': item, 'minItems': len(ids), 'maxItems': len(ids)}},
              'required': ['queries'], 'additionalProperties': False}
    system = ('Plan literature retrieval for TWO neighboring claims and their local connection only. '
        'Return one Europe PMC Boolean query and one short OpenAlex plain-text query for every supplied target_id. '
        'Search each qualified claim and the shared entity/condition compatibility; do not plan a complete upstream '
        'or downstream chain and do not invent a direct first-to-last effect. Use neutral scientific terms so '
        'supporting, negative and opposing findings remain discoverable. Keep essential conditions attached to '
        'the relevant entity/link, never search a condition alone. Use aliases supported by supplied context. '
        'OpenAlex queries use 3-12 discriminating words without Boolean syntax. Return JSON only.')
    system += (' Preserve the exact outcome, negation, magnitude and descriptive restrictions. '
        'Search the measured endpoint and relevant biological setting, rather than only the disease/entity name. '
        'Seek neutral evidence that can distinguish the original assertion from a broader or corrected statement. '
        'Context from the source defines meaning; finding the same source again is not independent confirmation.')
    system += (' Search scientifically equivalent terminology using only evidence-grounded aliases. '
        'Keep population, period and denominator for numerical assertions. '
        'When a claim involves demographic, historical, or quantitative population impacts (such as famine mortality, '
        'displacement, or population fractions), include explicit demographic search terms (e.g. "mortality" OR "deaths" '
        'OR "emigration" OR "population loss" alongside the pathogen and region) so demographic historical records '
        'are retrieved alongside molecular pathology. For exclusion links retrieve '
        'the exclusion and downstream relationship without assuming positive propagation; for host/context '
        'links search local host/material compatibility rather than asking whether the host property '
        'itself caused the outcome. Do not invent intermediate mechanisms or silently broaden restrictions.')
    contexts = list(dict.fromkeys(r['claim'].get('source_local_context') or r['claim'].get('source_evidence_span') or ''
                                 for r in target['source_claims']))
    payload = {'pathway_id': target['pathway_id'], 'retrieval_targets': missing, 'source_context': contexts}
    def check(answer):
        supplied = [r['target_id'] for r in answer['queries']]
        if sorted(supplied) != sorted(ids):
            raise IncompleteResponseError('Plan must cover every requested local target exactly once')
        if any(not r['query'].strip() or not r['openalex_query'].strip() for r in answer['queries']):
            raise IncompleteResponseError('Pathway query is empty')
    answer = client.chat_json(system, json.dumps(payload, ensure_ascii=False), max_tokens=4096,
        json_schema=schema, request_label=f"neighbor query: {target['pathway_id']}", response_check=check)
    check(answer)
    return {'queries': [*retained.values(), *answer['queries']]}


def followup_queries(client, target, assessment):
    pending={r['edge_id'] for r in assessment.get('edge_assessments',[]) if r['stance']!='supported'}
    pending.update(r['connection_id'] for r in assessment.get('connection_assessments',[])
                   if r['compatibility']!='compatible')
    pending.update(r['edge_id'] for r in assessment.get('corrections',[]))
    facets = [r for r in search_targets(target) if r['target_id'] in pending]
    if not facets:
        return {'queries':[]}
    schema = {'type': 'object', 'properties': {'queries': {'type': 'array', 'minItems': 1, 'maxItems': 3,
        'items': {'type': 'object', 'properties': {'target_id': {'type': 'string', 'enum': [r['target_id'] for r in facets]},
            'query': {'type': 'string', 'minLength': 1, 'maxLength': 1000},
            'openalex_query': {'type': 'string', 'minLength': 1, 'maxLength': 240}},
            'required': ['target_id','query','openalex_query'], 'additionalProperties': False}}},
        'required': ['queries'], 'additionalProperties': False}
    def check(answer):
        if any(r['target_id'] not in pending or not r['query'].strip() or not r['openalex_query'].strip()
               for r in answer['queries']):
            raise IncompleteResponseError('Follow-up queries must be nonempty and address only pending local targets')
    answer = client.chat_json('Plan ONE bounded follow-up literature search for the unresolved local claim/connection conditions '
        'of this neighboring pair only. Do not search the complete upstream/downstream chain. Return 1-3 complementary neutral queries anchored to the relevant entities. '
        'Do not search conditions alone. Do not invent missing facts or claim the pathway is supported. '
        'Do not repeat established upstream relationships. Target the unresolved outcome, its metric, magnitude, '
        'biological setting or proposed terminology correction. Query wording must not assume the verdict. '
        'Europe PMC query is Boolean; OpenAlex query is short plain text. JSON only.',
        json.dumps({'pathway': target['description'], 'retrieval_targets': facets, 'assessment': assessment,
                    'source_context':list(dict.fromkeys(r['claim'].get('source_local_context') or r['claim'].get('source_evidence_span') or ''
                                                     for r in target['source_claims']))}, ensure_ascii=False),
        max_tokens=1500, json_schema=schema, request_label=f"neighbor follow-up query: {target['pathway_id']}",response_check=check)
    check(answer)
    return answer
