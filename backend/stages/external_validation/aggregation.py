"""Aggregate admitted paper findings without making a new scientific judgement."""
from backend.config.settings import SCHEMA_VERSION
from backend.shared.storage import utc_now


def aggregate_assessment(candidate, result, decisions, signature, mode):
    attempts = [{"attempt": 1, **result, "decisions": decisions}]
    document_by_id = {document["evidence_document_id"]: document for document in result["evidence_documents"]}
    support_ids = {d.get("evidence_document_id") for d in decisions if d.get("decision") == "supported"}
    reference_support_ids = {identifier for identifier in support_ids
                             if document_by_id.get(identifier, {}).get("evidence_level") == "source_reference"}
    independent_support_ids = {identifier for identifier in support_ids
                               if document_by_id.get(identifier, {}).get("evidence_level") == "independent_external"}
    independent_document_groups = {document_by_id[identifier].get("independence_group") for identifier in independent_support_ids}
    independent_groups = ({document_by_id[d['evidence_document_id']].get('independence_group') for d in decisions
                           if d.get('evidence_document_id') in independent_support_ids
                           and (d.get('finding') or {}).get('evidence_origin') == 'original_result'}
                          if mode in {'per_paper_synthesis', 'trust_graph'} else independent_document_groups)
    contradiction_ids = {d.get("evidence_document_id") for d in decisions if d.get("decision") == "contradicted"}
    if ((result.get('joint_assessment') or {}).get('semantic_assessment') or {}).get('in_scope') is False:
        status = 'out_of_scope'
    elif (result.get('joint_assessment') or {}).get('semantic_review_required'):
        status = 'external_support_needs_review' if support_ids else 'external_assessment_needs_review'
    elif support_ids and contradiction_ids:
        status = "world_contested_pending_review"
    elif len(independent_groups) >= 2:
        status = "world_independently_corroborated_pending_review"
    elif independent_support_ids:
        status = "world_independent_supported_pending_corroboration"
    elif reference_support_ids:
        status = "source_reference_supported_pending_independent_corroboration"
    elif contradiction_ids:
        status = "world_contradicted_or_contested"
    elif any(d.get('decision') == 'needs_review' for d in decisions):
        status = 'external_assessment_needs_review'
    elif attempts[0]["evidence_documents"]:
        status = "external_evidence_not_found"
    else:
        status = "retrieval_failed_or_no_open_access_evidence"
    if result.get('retrieval_pending'):
        status = 'retrieval_pending'
    return {"schema_version": SCHEMA_VERSION, "candidate_id": candidate["candidate_id"], "candidate": candidate,
            "validation_signature": signature, "retrieval_signature": result.get("retrieval_signature"),
            "attempts": attempts, "validation_status": status,
            "support_summary": {"source_reference": len(reference_support_ids),
                                "independent_external": len(independent_support_ids),
                                "independent_groups": len(independent_groups),
                                "independent_document_groups": len(independent_document_groups)},
            "validated_at": utc_now()}
