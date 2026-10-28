"""Read saved extractions globally, with document-scoped immutable edge references."""
import json

from backend.shared.storage import read_jsonl, stable_hash
from backend.stages.graph_construction.graph import _edge_status
from backend.domain.ontology import external_project_scope


ASSEMBLY_VERSION = 1
ASSEMBLY_DIRECTORY = '.pathways'
ASSEMBLY_ELIGIBILITY_POLICY = 'source-entailed-independent-external-support-v1'
SUPPORTED_EXTERNAL_STATUSES = {'world_independently_corroborated_pending_review',
                               'world_independent_supported_pending_corroboration'}


def collect_corpus(root, provisional=False):
    edges, endpoints = [], []
    for path in sorted(root.glob('*/candidates.jsonl')):
        if path.parent.name == ASSEMBLY_DIRECTORY:
            continue
        results = {r['candidate_id']: r for r in read_jsonl(path.parent / 'validation_runs.jsonl')}
        for c in read_jsonl(path):
            if c.get('schema_error') or not all(c.get(k) for k in (
                    'candidate_id', 'subject', 'relation', 'object', 'subject_type', 'object_type')):
                continue
            result = results.get(c['candidate_id'], {})
            status, support, contradict, reference_support = _edge_status(c, result)
            # Require a PDF fidelity pass and accepted independent external
            # support. Other evidence states remain in document audit views.
            if ((c.get('source_validation') or {}).get('decision') != 'entailed'
                    or (not provisional and (external_project_scope(result) is not True
                    or result.get('validation_status') not in SUPPORTED_EXTERNAL_STATUSES
                    or not (support - reference_support)
                    or status not in {'single_source', 'corroborated'}))):
                continue
            edge_id = 'edge-' + stable_hash(f"{path.parent.name}|{c['candidate_id']}", 28)
            edge = dict(id=edge_id, candidate_id=c['candidate_id'], document_key=path.parent.name,
                        paper=c.get('source_paper_title') or path.parent.name,
                        source_document_id=c.get('source_document_id'), source_pdf_sha256=c.get('source_pdf_sha256'),
                        subject=c['subject'], object=c['object'], relation=c['relation'],
                        subject_type=c['subject_type'], object_type=c['object_type'],
                        source_doi=c.get('source_doi', ''),
                        source_evidence_sentence_numbers=c.get('source_evidence_sentence_numbers', []),
                        subject_source_mention=c.get('subject_source_mention', ''),
                        object_source_mention=c.get('object_source_mention', ''),
                        claim_text=c.get('claim_text', ''), certainty=c.get('certainty', ''),
                        qualifiers=c.get('qualifiers') or {}, quote=c.get('source_evidence_span') or c.get('source_evidence_quote', ''),
                        source_page=c.get('source_page'), source_status=(c.get('source_validation') or {}).get('decision', 'not_run'),
                        external_status=results.get(c['candidate_id'], {}).get('validation_status', 'not_evaluated'),
                        status=status, support_count=len(support), contradiction_count=len(contradict),
                        causal_chain_validated=False, **({'provisional': True} if provisional else {}))
            edges.append(edge)
            for role in ('subject', 'object'):
                endpoints.append(dict(endpoint_id=f'{edge_id}:{role}', edge_id=edge_id, role=role,
                    name=c[role], entity_type=c[f'{role}_type'], source_mention=c.get(f'{role}_source_mention', ''),
                    document_key=path.parent.name, paper=edge['paper'], source_page=edge['source_page'],
                    triplet={k:c[k] for k in ('subject', 'relation', 'object')}, qualifiers=edge['qualifiers'],
                    evidence=edge['quote'], local_context=c.get('source_local_context') or edge['quote'],
                    section=c.get('source_evidence_section_heading', ''), certainty=edge['certainty']))
    return edges, endpoints


def corpus_signature(edges, endpoints, model, thinking=True, *, provisional=False):
    if provisional:
        edges = [{k: v for k, v in edge.items() if k not in {'external_status', 'status', 'support_count', 'contradiction_count'}} for edge in edges]
    return stable_hash(json.dumps(dict(version=ASSEMBLY_VERSION, eligibility_policy='source-entailed-provisional-v1' if provisional else ASSEMBLY_ELIGIBILITY_POLICY,
                                  model=model, edges=edges, endpoints=endpoints, **({} if thinking else {'thinking': 'disabled'})),
                                  sort_keys=True, ensure_ascii=False), 64)
