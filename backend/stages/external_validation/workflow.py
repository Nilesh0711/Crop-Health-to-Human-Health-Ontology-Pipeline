"""Coordinate one joint external assessment and aggregate guarded paper findings."""

from __future__ import annotations

import json
import time
from copy import deepcopy
from dataclasses import replace
from typing import Any, Callable
from backend.config.settings import SCHEMA_VERSION, Settings
from backend.shared.cache import RunCache
from backend.providers.llm import OpenCodeClient
from backend.stages.external_validation.aggregation import aggregate_assessment
from backend.stages.external_validation.joint import judge_evidence_pack
from backend.stages.external_validation.synthesis import judge_with_synthesis, POLICY
from backend.providers.nli import NLIValidator
from backend.stages.evidence_ranking.passages import SemanticReranker
from backend.shared.storage import stable_hash, utc_now
from backend.stages.literature_retrieval.workflow import retrieve_round


def _signature(candidate: dict[str, Any], settings: Settings, top_k=None, top_chunks=None, **policy) -> str:
    fields = ("candidate_id", "subject", "relation", "object", "subject_type", "object_type", "certainty",
              "qualifiers", "source_pdf_sha256", "source_evidence_span", "repair_revision",
              "subject_source_mention", "object_source_mention", "source_references", "source_citation_links", "source_chapter_id")
    return stable_hash(json.dumps({"claim": {key: candidate.get(key) for key in fields},
                                  "schema": SCHEMA_VERSION, "models": {k:v for k,v in settings.task_models.items()
                                                                       if k != 'pathway_assembly'},
                                  "nli": [settings.enable_nli, settings.nli_model, settings.enable_nli_promotion],
                                  "selection": [top_k or settings.external_top_k, top_chunks or settings.external_top_chunks],
                                  "joint": True, **policy,
                                  **({'trust_retrieval_policy': 'table-context-v1'} if settings.external_judgement_mode == 'trust_graph' else {}),
                                  **({"thinking": {task: False for task in ("query", "external") if not settings.task_thinking[task]}} if any(not settings.task_thinking[task] for task in ("query", "external")) else {})}, sort_keys=True), 32)


def validation_signature(candidate, settings, top_k=None, top_chunks=None):
    policy = {} if settings.external_judgement_mode == 'joint' else {'judgement_policy': POLICY, 'paper_model': settings.external_paper_model,
        **({'trust_policy': 'evidence-balance-v1'} if settings.external_judgement_mode == 'trust_graph' else {}),
        'source_scope_context': stable_hash(json.dumps([candidate.get('source_evidence_span'), candidate.get('source_local_context')]), 32)}
    return _signature(candidate, settings, top_k, top_chunks, **policy)


def reusable_evidence(candidate, settings, record, top_k=None, top_chunks=None):
    retrieval_settings = replace(settings, thinking={**settings.task_thinking, 'external': True})
    compatible = {_signature(candidate, retrieval_settings, top_k, top_chunks),
                  validation_signature(candidate, retrieval_settings, top_k, top_chunks)}
    return bool(record and record.get('attempts') and not record['attempts'][-1].get('retrieval_pending') and
                record.get('retrieval_signature', record.get('validation_signature')) in compatible)


def prepare_evidence(candidate, client, reranker, settings, articles_per_candidate=None, top_chunks=None,
                     progress=None, query_translation=None, cache=None, resume_record=None, on_document=None):
    articles_per_candidate = articles_per_candidate or settings.external_top_k
    top_chunks = top_chunks or settings.external_top_chunks
    signature = validation_signature(candidate, settings, articles_per_candidate, top_chunks)
    # Retrieval is independent of judge architecture/Thinking. Preserve the
    # historical retrieval signature so saved packets need not be refetched.
    retrieval_signature = _signature(candidate, replace(settings, thinking={**settings.task_thinking, 'external': True}), articles_per_candidate, top_chunks)
    reusable = reusable_evidence(candidate, settings, resume_record, articles_per_candidate, top_chunks)
    if reusable:
        result = deepcopy(resume_record['attempts'][-1])
        result.pop('decisions', None)
        if progress:
            progress('Resuming retained evidence packet; compatible paper judgements will be reused')
    else:
        result = retrieve_round(candidate, client, reranker, settings, articles_per_candidate, top_chunks, progress,
                                query_translation=query_translation, cache=cache,
                                **({'on_document': on_document} if on_document else {}))
    if reusable and on_document:
        for document in result['evidence_documents']:
            on_document(document)
    result['retrieval_signature'] = retrieval_signature
    result['prepared_for_candidate_id'] = candidate['candidate_id']
    result['prepared_validation_signature'] = signature
    return result


def validate_candidate(candidate: dict[str, Any], client: OpenCodeClient, reranker: SemanticReranker, settings: Settings,
                       articles_per_candidate: int | None = None, top_chunks: int | None = None,
                       progress: Callable[[str], None] | None = None,
                       nli_validator: NLIValidator | None = None,
                       query_translation: dict[str, Any] | None = None,
                       cache: RunCache | None = None,
                       on_checkpoint: Callable[[dict[str, Any]], None] | None = None,
                       resume_record: dict[str, Any] | None = None,
                       prepared_evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    if progress:
        progress("External validation")
    articles_per_candidate = articles_per_candidate or settings.external_top_k
    top_chunks = top_chunks or settings.external_top_chunks
    signature = validation_signature(candidate, settings, articles_per_candidate, top_chunks)
    if prepared_evidence is not None and (prepared_evidence.get('prepared_for_candidate_id') != candidate['candidate_id']
            or prepared_evidence.get('prepared_validation_signature') != signature):
        raise ValueError('Prepared evidence does not match this claim and validation configuration')
    result = prepared_evidence if prepared_evidence is not None else prepare_evidence(
        candidate, client, reranker, settings, articles_per_candidate, top_chunks, progress,
        query_translation, cache, resume_record)
    documents = result["evidence_documents"]
    mode = settings.external_judgement_mode
    result['judge_allocation'] = {'policy': POLICY if mode == 'per_paper_synthesis' else 'joint_multi_paper',
                                'model': getattr(client, 'model', None), 'thinking': settings.task_thinking['external'],
                                'phase_models': settings.external_phase_models if mode == 'per_paper_synthesis' else None}
    result.pop('joint_assessment', None)

    def checkpoint(status, decisions, error=None):
        if on_checkpoint:
            on_checkpoint(deepcopy({'schema_version': SCHEMA_VERSION, 'candidate_id': candidate['candidate_id'],
                'validation_signature': signature, 'retrieval_signature': result.get('retrieval_signature'), 'validation_status': status, 'error': error,
                'attempts': [{'attempt': 1, **result, 'decisions': decisions}], 'validated_at': utc_now()}))

    checkpoint('external_in_progress', [])
    if documents:
        judge_started = time.monotonic()
        try:
            if mode == 'per_paper_synthesis':
                def save_papers(records):
                    result['paper_judgements'] = records
                    assessed = {row['evidence_document_id'] for row in records}
                    for reference in result.get('citation_ledger', []):
                        if assessed.intersection(reference.get('evidence_document_ids') or []):
                            reference.update(status='included_in_judgement', included_in_judgement=True)
                    checkpoint('external_in_progress', [row['decision'] for row in records])
                decisions, assessment = judge_with_synthesis(client, candidate, documents, settings,
                    saved_records=result.get('paper_judgements'), on_paper=save_papers, progress=progress, nli_validator=nli_validator)
            else:
                decisions, assessment = judge_evidence_pack(client, candidate, documents, settings, nli_validator)
        except Exception as exc:
            result.setdefault('timings_seconds', {})['judge_and_proof_checks'] = round(time.monotonic() - judge_started, 3)
            checkpoint('pipeline_error', [row['decision'] for row in result.get('paper_judgements', [])]
                       if mode == 'per_paper_synthesis' else [], str(exc))
            raise
        result.setdefault('timings_seconds', {})['judge_and_proof_checks'] = round(time.monotonic() - judge_started, 3)
        result['joint_assessment'] = assessment
        for row in result.get('citation_ledger', []):
            if row['fetched']:
                row.update(status='included_in_judgement', included_in_judgement=True)
        seen_quotes = set()
        contributions = []
        for decision in decisions:
            quotes = {' '.join(e.get('quoted_span', '').casefold().split()) for e in decision.get('evidence', []) if e.get('quoted_span')}
            novel = quotes - seen_quotes
            seen_quotes.update(quotes)
            contributions.append({'evidence_document_id': decision['evidence_document_id'],
                                  'decision': decision['decision'], 'admissible_quotes': len(quotes),
                                  'new_quote_texts': len(novel),
                                  'contribution': 'guarded_finding' if decision['decision'] in {'supported', 'contradicted'} else 'no_admissible_decisive_finding'})
        result['paper_contributions'] = contributions
        if progress:
            progress(f"External validation completed: {len(documents)} papers; {result['judge_allocation']['policy']}")
        checkpoint('external_in_progress', decisions)
    else:
        decisions = []
    return aggregate_assessment(candidate, result, decisions, signature, mode)


def evidence_ledger(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ledger: list[dict[str, Any]] = []
    for record in records:
        decisions = [d for attempt in record.get("attempts", []) for d in attempt.get("decisions", [])]
        supports = {d.get("evidence_document_id") for d in decisions if d.get("decision") == "supported"}
        reference_supports = {d.get("evidence_document_id") for d in decisions
                              if d.get("decision") == "supported" and d.get("evidence_level") == "source_reference"}
        independent_supports = supports - reference_supports
        contradictions = {d.get("evidence_document_id") for d in decisions if d.get("decision") == "contradicted"}
        ledger.append({"schema_version": SCHEMA_VERSION, "candidate_id": record["candidate_id"],
                       "candidate": record.get("candidate", {}),
                       "evidence_decisions": decisions, "support_count": len(supports),
                       "source_reference_support_count": len(reference_supports),
                       "independent_support_count": len(independent_supports),
                       "contradiction_count": len(contradictions),
                       "world_validation_status": record["validation_status"], "updated_at": utc_now()})
    return ledger
