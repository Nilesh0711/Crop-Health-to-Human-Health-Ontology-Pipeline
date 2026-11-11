"""Ground saved claims and index mappings; downstream stages can stay paused."""
import json
from collections import Counter

from rdflib import Graph, URIRef, Literal
from rdflib.namespace import RDF, RDFS, XSD

from backend.ontology.catalog import catalog as load_catalog, LOCAL
from backend.ontology.signatures import inventory_signature
from backend.ontology.registry import entity_registry
from backend.stages.ontology_grounding.grounder import ground_claims, claim_signature, GROUNDING_POLICY
from backend.stages.ontology_grounding.grouping import evidence_groups, group_signature
from backend.stages.ontology_reasoning.engine import reason
from backend.shared.artifacts import save_json
from backend.shared.storage import read_jsonl, stable_hash, utc_now
from backend.shared.cancellation import check_cancelled
from backend.stages.claim_extraction.contract import valid_relation_wording
from backend.config.pipeline import POST_GROUNDING_STAGES_PAUSED, POST_GROUNDING_PAUSED_MESSAGE, stage_status

DIRECTORY = '.ontology_grounding'


def current_assertions(root, reference):
    rows, stale = [], []
    for path in sorted(root.glob('*/ontology_grounding.jsonl')):
        candidates = {c['candidate_id']: c for c in read_jsonl(path.parent / 'candidates.jsonl')}
        group_signatures = {}
        for row in read_jsonl(path):
            candidate = candidates.get(row['candidate_id'])
            maximum = row.get('group_size_limit', 3)
            if maximum not in group_signatures:
                valid = [c for c in candidates.values() if not c.get('schema_error') and not c.get('superseded_by')]
                group_signatures[maximum] = {c['candidate_id']: group_signature(group)
                    for group in evidence_groups(valid, maximum) for c in group}
            if (candidate is None or candidate.get('schema_error') or candidate.get('superseded_by')
                    or row.get('catalog_signature') != reference.signature
                    or row.get('policy') != GROUNDING_POLICY
                    or row.get('group_signature') != group_signatures[maximum].get(row['candidate_id'])
                    or row.get('claim_signature') != claim_signature(candidate)):
                stale.append({'candidate_id': row['candidate_id'], 'document_key': path.parent.name})
            else:
                rows.append(row)
    return rows, stale


def rebuild(root, reference=None):
    reference = reference or load_catalog()
    rows, stale = current_assertions(root, reference)
    registry, edges = entity_registry(rows), []
    for row in ([] if POST_GROUNDING_STAGES_PAUSED else rows):
        if not row['assembly_eligible']:
            continue
        claim = row['original_claim']
        edges.append({'id': 'edge-' + stable_hash(row['document_key'] + '|' + row['candidate_id'], 28),
            'candidate_id': row['candidate_id'], 'document_key': row['document_key'],
            'source': row['bindings']['subject']['entity_id'], 'target': row['bindings']['object']['entity_id'],
            'subject': claim['subject'], 'object': claim['object'], 'relation': claim['relation'],
            'source_label': row['bindings']['subject']['label'], 'target_label': row['bindings']['object']['label'],
            'relation_iri': row['bindings']['relation']['iri'], 'claim_text': claim.get('claim_text'),
            'quote': claim.get('source_evidence_span'), 'source_page': claim.get('source_page'),
            'qualifiers': claim.get('qualifiers') or {},
            'status': 'unvalidated', 'source_status': 'not_assessed', 'external_status': 'not_assessed',
            'validation_stages_paused': True, 'causal_chain_validated': False})
    node_ids = {e[role] for e in edges for role in ('source', 'target')}
    graph_nodes = [{**e, 'type': 'OntologyGrounded', 'occurrence_ids': [o['id'] for o in e['occurrences']]}
                   for e in registry if e['id'] in node_ids]
    reasoning = ({'status': 'paused', 'message': POST_GROUNDING_PAUSED_MESSAGE,
                  'facts': [], 'issues': [], 'counts': {'asserted': 0, 'derived': 0, 'issues': 0},
                  'coverage': {'assessed': False, 'supported_fragment': [], 'complete_owl_consistency': False}}
                 if POST_GROUNDING_STAGES_PAUSED else reason(reference, rows))
    state = {'version': 2, 'policy': 'ontology-grounded-identity-v2', 'completed_at': utc_now(),
        'catalog_signature': reference.signature, 'rules_signature': reference.rules_signature,
        'source_inventory': inventory_signature(root), 'profile': reference.profile,
        'assertions': rows, 'stale_assertions': stale,
        'entities': registry,
        'counts': {'claims': len(rows), 'entities': len(registry),
                   'mapped_claims': sum(r['assembly_eligible'] for r in rows),
                   'grounded_entities': sum(e['status'] == 'grounded' for e in registry),
                   'grounded_mentions': sum(len(e['occurrences']) for e in registry if e['status'] == 'grounded'),
                   'vocabulary_gaps': sum(len(e['occurrences']) for e in registry if e['status'] == 'vocabulary_gap'),
                   'broader_classification_mentions': sum(len(e['occurrences']) for e in registry if e['mapping'] == 'broader'),
                   'related_narrower_review_mentions': sum(len(e['occurrences']) for e in registry if e['mapping'] in {'related', 'narrower'}),
                   'shared_concepts': sum(e['shared_concept'] and len(e['occurrences']) > 1 for e in registry),
                   'papers': len({r['document_key'] for r in rows}),
                   'scope': dict(Counter(r['scope'] for r in rows)),
                   'needs_mapping_review': sum(not r['assembly_eligible'] and r['scope'] != 'out_of_scope' for r in rows)},
        'graph': {'nodes': graph_nodes, 'edges': edges,
                  'counts': {'nodes': len(graph_nodes), 'edges': len(edges), 'supported': 0, 'corroborated': 0}},
        'validation_stages_paused': True, 'causal_chain_validated': False,
        'post_grounding_stages_paused': POST_GROUNDING_STAGES_PAUSED, 'stage_status': stage_status(),
        'reasoning_counts': reasoning['counts'], 'reasoning_coverage': reasoning['coverage']}
    folder = root / DIRECTORY
    folder.mkdir(parents=True, exist_ok=True)
    save_json(folder / 'snapshot.json', state)
    if not POST_GROUNDING_STAGES_PAUSED:
        save_json(folder / 'reasoning.json', reasoning)
        export_rdf(folder / 'assertions.ttl', reference, rows)
    return state, reasoning


def export_rdf(path, reference, rows):
    """Reify study assertions; never assert conditional class-level causal triples."""
    graph = Graph()
    graph.bind('app', LOCAL)
    for row in rows:
        identifier = URIRef(LOCAL + 'assertion-' + stable_hash(row['document_key'] + '|' + row['candidate_id'], 32))
        graph.add((identifier, RDF.type, URIRef(LOCAL + 'ScientificAssertion')))
        graph.add((identifier, RDF.type, RDF.Statement))
        original = row['original_claim']
        graph.add((identifier, URIRef(LOCAL + 'qualifiers'), Literal(json.dumps(original.get('qualifiers') or {}, sort_keys=True))))
        if original.get('certainty'):
            # Keep historical annotations without manufacturing labels for new claims.
            graph.add((identifier, URIRef(LOCAL + 'certainty'), Literal(original['certainty'])))
        passage = URIRef(str(identifier) + '-passage')
        graph.add((passage, RDF.type, URIRef(LOCAL + 'SourcePassage')))
        graph.add((passage, URIRef(LOCAL + 'sourceText'), Literal(original.get('source_evidence_span') or '')))
        graph.add((identifier, URIRef(LOCAL + 'usesEvidence'), passage))
        graph.add((passage, URIRef(LOCAL + 'sourceDocument'), Literal(row['document_key'])))
        if type(original.get('source_page')) is int:
            graph.add((passage, URIRef(LOCAL + 'sourcePage'), Literal(original['source_page'], datatype=XSD.integer)))
        for citation in original.get('source_references', []):
            graph.add((passage, URIRef(LOCAL + 'citationText'), Literal(json.dumps(citation, ensure_ascii=False, sort_keys=True))))
        graph.add((identifier, URIRef(LOCAL + 'originalRelation'), Literal(original['relation'])))
        for role, predicate in (('subject', RDF.subject), ('object', RDF.object)):
            b = row['bindings'][role]
            occurrence = URIRef(LOCAL + b['occurrence_id'])
            graph.add((occurrence, RDF.type, URIRef(LOCAL + 'Mention')))
            graph.add((occurrence, RDFS.label, Literal(original[role])))
            if original.get(role + '_context'):
                graph.add((occurrence, RDFS.comment, Literal('Endpoint context: ' + original[role + '_context'])))
            graph.add((identifier, predicate, occurrence))
            if b['mapping'] == 'exact' and b['iri']:
                graph.add((occurrence, URIRef(LOCAL + 'denotesConcept'), URIRef(b['iri'])))
        relation = row['bindings']['relation']
        if relation['mapping'] == 'exact' and relation['iri']:
            graph.add((identifier, RDF.predicate, URIRef(relation['iri'])))
    temporary = path.with_suffix('.ttl.tmp')
    graph.serialize(destination=str(temporary), format='turtle')
    temporary.replace(path)


def run_grounding(root, output, candidates, client, settings, emit, *, reference=None, encoder=None):
    if any(not valid_relation_wording(c.get('relation')) for c in candidates):
        raise ValueError('Saved extraction contains incomplete relation labels; start a new run to extract complete predicates')
    reference = reference or load_catalog()
    emit('stage', 'Grounding potato soil-nutrition claims against frozen established ontology definitions')
    emit('info', f'Ontology grounding uses {client.model}; primary model only; '
         f'{getattr(client.settings, "request_timeout_seconds", settings.ontology_grounding_timeout_seconds)}s timeout; '
         f'{settings.ontology_grounding_batch_size} claims per batch; '
         f'up to {settings.ontology_grounding_workers} requests in parallel; '
         f'output={settings.ontology_grounding_output_format}')
    emit('info', f'All {len(reference.profile["allowed_classes"])} approved classes and '
         f'{len(reference.profile["allowed_object_properties"])} relationship properties are supplied; no class shortlisting')
    rows = ground_claims(candidates, output, reference, client, encoder=encoder,
                        batch_size=getattr(settings, 'ontology_grounding_batch_size', 1),
                        workers=settings.ontology_grounding_workers, emit=emit)
    check_cancelled()
    if POST_GROUNDING_STAGES_PAUSED:
        emit('info', POST_GROUNDING_PAUSED_MESSAGE)
    else:
        emit('stage', 'Reasoning over imported named axioms and reviewed context-guarded rules')
    state, reasoning = rebuild(root, reference)
    local_registry = entity_registry(rows)
    summary = {'scope': dict(Counter(r['scope'] for r in rows)), 'claims': len(rows),
               'mapped_claims': sum(r['assembly_eligible'] for r in rows),
               'needs_review': sum(not r['assembly_eligible'] and r['scope'] != 'out_of_scope' for r in rows),
               'catalog_signature': reference.signature, 'profile_id': reference.profile['id'],
               'grounded_entities': sum(e['status'] == 'grounded' for e in local_registry),
               'grounded_mentions': sum(len(e['occurrences']) for e in local_registry if e['status'] == 'grounded'),
               'shared_concepts': sum(e['shared_concept'] and len(e['occurrences']) > 1 for e in local_registry),
               'vocabulary_gaps': sum(len(e['occurrences']) for e in local_registry if e['status'] == 'vocabulary_gap'),
               'global_registry_counts': state['counts'],
               'reasoning': reasoning['counts'], 'reasoning_coverage': reasoning['coverage'],
               'post_grounding_stages_paused': POST_GROUNDING_STAGES_PAUSED,
               'stage_status': stage_status()}
    save_json(output / 'ontology_grounding_summary.json', summary)
    emit('success', f'Grounding complete: {summary["grounded_mentions"]} endpoint mentions grounded independently; '
         f'{summary["mapped_claims"]}/{len(rows)} complete claims have exact in-scope mappings; '
         f'{summary["needs_review"]} need review. Scientific validation remains paused.')
    return state, summary
