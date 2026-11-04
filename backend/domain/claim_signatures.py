"""Scientific identity of saved legacy records; no numerical scoring."""
import json
from backend.shared.storage import stable_hash


def scientific_signature(candidate):
    fields = {k: candidate.get(k) for k in ('candidate_id', 'subject', 'relation', 'object', 'subject_type', 'object_type', 'qualifiers',
              'certainty', 'claim_text', 'source_pdf_sha256', 'source_evidence_span', 'source_local_context',
              'source_section_context', 'repair_revision')}
    return stable_hash(json.dumps(fields, sort_keys=True, ensure_ascii=False), 64)


def assessment_fingerprint(result):
    state = {'signature': result.get('validation_signature'), 'status': result.get('validation_status'),
             'attempts': [{'decisions': a.get('decisions'),
                           'semantic': (a.get('joint_assessment') or {}).get('semantic_assessment'),
                           'documents': [{k: d.get(k) for k in ('evidence_document_id', 'doi', 'evidence_chunks')}
                                         for d in a.get('evidence_documents') or []]} for a in result.get('attempts') or []]}
    return stable_hash(json.dumps(state, sort_keys=True, ensure_ascii=False), 64)
