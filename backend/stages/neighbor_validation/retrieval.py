"""Shared discovery for two neighboring claims, with no full-chain target."""
import json
from backend.shared.storage import stable_hash
from backend.stages.neighbor_validation.targets import search_targets
from backend.stages.literature_retrieval.workflow import retrieve_round


def retrieval_candidate(target):
    references, seen, origins, links, link_keys = [], set(), {}, [], set()
    for original in target['source_claims']:
        for reference in original['claim'].get('source_references') or []:
            key = json.dumps(reference, sort_keys=True)
            if key not in seen:
                seen.add(key); references.append(reference)
            identifier = stable_hash(key,24)
            origins.setdefault(identifier,[]).append({'document_key':original['document_key'],
                'edge_id':original['edge_id'],'candidate_id':original['claim']['candidate_id']})
        for link in original['claim'].get('source_citation_links') or []:
            key = original['document_key'] + json.dumps(link,sort_keys=True)
            if key not in link_keys:
                link_keys.add(key)
                links.append({**link,'document_key':original['document_key'],'edge_id':original['edge_id']})
    first, last = (target['source_claims'][i]['claim'] for i in (0, -1))
    return {'candidate_id': target['pathway_id'], 'pathway_target': True,
            'pathway_description': target['description'], 'ranking_targets': search_targets(target),
            # These endpoints are transport hints for the shared retrieval adapter,
            # never a new A->C claim or a scientific assessment target.
            'subject': first['subject'], 'object': last['object'], 'relation': 'proposed_pathway',
            'subject_type': first['subject_type'], 'object_type': last['object_type'], 'qualifiers': {},
            'source_references': references, 'citation_origins':origins, 'source_citation_links':links,
            'originating_sources': [r['claim'] for r in target['source_claims']]}


def retrieve_pathway(target, plan, client, reranker, settings, cache, progress=None, on_document=None):
    candidate=retrieval_candidate(target)
    requested={q['target_id'] for q in plan['queries']}
    candidate['ranking_targets']=[facet for facet in candidate['ranking_targets'] if facet['target_id'] in requested]
    if not candidate['ranking_targets']:
        raise ValueError('Retrieval must address at least one existing neighboring claim or connection')
    result = retrieve_round(candidate, client, reranker, settings,
        settings.external_top_k, settings.external_top_chunks, progress, query_translation=plan, cache=cache, on_document=on_document)
    present = {facet for d in result['evidence_documents'] for c in d['evidence_chunks'] for facet in c.get('retrieval_target_ids', [])}
    result['pathway_coverage'] = {'retrieval_targets': candidate['ranking_targets'],
        'targets_without_retained_passages': [r['target_id'] for r in candidate['ranking_targets'] if r['target_id'] not in present],
        'ranking_is_scientific_proof': False}
    return result
