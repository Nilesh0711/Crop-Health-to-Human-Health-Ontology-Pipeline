"""Coordinate the PDF stages, checkpoints, model metrics and final run artifacts."""

from __future__ import annotations

import json
import time
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from pypdf import PdfReader
from backend.config.settings import ONTOLOGY_DIR, SCHEMA_VERSION, Settings
from backend.shared.batching import iter_batched_judgments
from backend.shared.cache import RunCache
from backend.pipelines.external import prefetch_evidence
from backend.shared.cancellation import check_cancelled
from backend.shared.activity import TaggedMessage
from backend.providers.llm import (
    LLMServiceError,
    OpenCodeClient,
    format_request_metrics,
    model_reasoning_policy,
    request_log_level,
)
from backend.providers.nli import NLIValidator
from backend.stages.evidence_ranking.passages import SemanticReranker
from backend.shared.storage import file_hash, utc_now, read_jsonl
from backend.shared.artifacts import save_rows
from backend.pipelines.checkpoints import claim_stage_signature, resume_metadata, save_checkpoint
from backend.stages.query_planning.planner import translate_queries
from backend.stages.external_validation.workflow import validate_candidate, prepare_evidence, validation_signature
from backend.stages.graph_construction.graph import graph_from_candidates, reasoning_paths, write_graphml
from backend.stages.claim_extraction.deduplication import deduplicate_source_claims
from backend.domain.claim_context import apply_source_provenance
from backend.stages.evaluation.metrics import write_evaluation_sample
from backend.stages.pdf_parsing.quality import assess_text_quality
from backend.stages.source_validation.checker import validate_source_claim
from backend.stages.source_validation.abbreviations import SOURCE_VALIDATION_POLICY
from backend.stages.source_validation.context import section_context
from backend.domain.vocabulary import normalize_entity
from backend.stages.pdf_parsing.structure import inspect_pdf
from backend.stages.claim_extraction.extractor import extract_claims
from backend.stages.claim_extraction.targeting import TARGETING_VERSION
from backend.stages.pathway_assembly.assembly import assemble_pathways
from backend.stages.pdf_parsing.packets import (
    iter_pdf_context_packets,
    pdf_bibliography,
    source_article_identifiers,
)


def run_pipeline(pdf_path: Path, max_pages: int, max_claims: int, articles_per_claim: int,
                 emit: Callable[[str, str], None], claims_per_packet: int | None = None,
                 resume: bool = False, extraction_mode: str | None = None,
                 extraction_target_words: int | None = None,
                 extraction_generation_budget: int | None = None,
                 thinking: dict[str, bool] | None = None) -> dict[str, Any]:
    output_dir = ONTOLOGY_DIR / pdf_path.stem
    output_dir.mkdir(parents=True, exist_ok=True)
    run_started = time.monotonic()
    stage_started = run_started
    stage_timings: dict[str, float] = {}

    def finish_stage(name: str) -> None:
        nonlocal stage_started
        check_cancelled()
        now = time.monotonic()
        stage_timings[name] = round(now - stage_started, 3)
        stage_started = now
        emit("info", f"{name.replace('_', ' ').capitalize()} elapsed: {stage_timings[name]:.1f}s")

    settings = Settings()
    settings = replace(settings, thinking=thinking if thinking is not None else settings.thinking,
                       extraction_mode=extraction_mode if extraction_mode is not None else settings.extraction_mode,
                       extraction_target_words=(extraction_target_words if extraction_target_words is not None
                                                else settings.extraction_target_words),
                       extraction_min_output_budget=(extraction_generation_budget if extraction_generation_budget is not None
                                                     else settings.extraction_min_output_budget))
    model_requests: list[dict[str, Any]] = read_jsonl(output_dir / 'llm_requests.jsonl') if resume else []
    request_log_lock = threading.Lock()
    claims_per_packet = settings.extraction_claims_per_packet if claims_per_packet is None else claims_per_packet
    if not 0 <= claims_per_packet <= 8:
        raise ValueError("claims_per_packet must be between 0 and 8; 0 means all eligible claims")
    checkpoint = resume_metadata(output_dir, pdf_path, settings, max_pages, claims_per_packet,
                                 extraction_mode=settings.extraction_mode,
                                 target_words=settings.extraction_target_words) if resume else {
        'schema_version': SCHEMA_VERSION, 'pdf_sha256': file_hash(pdf_path), 'task_models': settings.task_models,
        'max_pages': max_pages, 'claims_per_packet': claims_per_packet, 'extraction_complete': False,
        'extraction_mode': settings.extraction_mode, 'extraction_target_words': settings.extraction_target_words,
        'extraction_generation_budget': settings.extraction_min_output_budget, 'targeting_version': TARGETING_VERSION}
    checkpoint['thinking'] = settings.task_thinking
    available_models = OpenCodeClient(settings).health_check().get("models", [])
    for task, model in settings.task_models.items():
        if model.split('#', 1)[0] not in available_models:
            raise RuntimeError(f"No configured OpenCode model is available for {task}: {model}")
    if settings.external_judgement_mode == 'per_paper_synthesis' and settings.external_paper_model not in available_models:
        raise RuntimeError(f'Configured per-paper primary is unavailable: {settings.external_paper_model}')
    def log_model_request(metrics: dict[str, Any]) -> None:
        with request_log_lock:
            model_requests.append({"at": utc_now(), **metrics})
            save_rows(output_dir / "llm_requests.jsonl", model_requests)
        emit(request_log_level(metrics), TaggedMessage(format_request_metrics(metrics), metrics.get("stage") or "system"))

    task_clients = {task: OpenCodeClient(
        replace(settings, request_timeout_seconds=settings.extraction_timeout_seconds) if task == 'extraction' else settings,
        model, task=task, on_request=log_model_request, on_progress=lambda event: emit("progress", json.dumps(event)),
        response_dir=output_dir / 'model_responses') for task, model in settings.task_models.items()}
    extractor, client = (task_clients[task] for task in ('extraction', 'external'))
    def llm_call_summary() -> dict[str, Any]:
        counters = {task: task_client.request_metrics for task, task_client in task_clients.items()}
        return {**counters, 'validation': counters['external'], 'helper': counters['query'],
                'total_requests': sum(item['requests'] for item in counters.values())}
    for task, model in settings.task_models.items():
        emit('info', f"LLM {task}: {model}; Thinking {'on' if settings.task_thinking[task] else 'off'}")
    emit('info', f"Extraction mode: {settings.extraction_mode}; claim limit per extraction region: {claims_per_packet}; "
         f"initial generation budget (reasoning + answer): {settings.extraction_min_output_budget} tokens")
    pdf_inspection = inspect_pdf(PdfReader(str(pdf_path)), max_pages)
    packets = list(iter_pdf_context_packets(pdf_path, max_pages, inspection=pdf_inspection))
    source_identifiers = source_article_identifiers(pdf_path)
    pdf_quality = {key: value for key, value in pdf_inspection.items() if key != 'regions'}
    save_rows(output_dir / 'pdf_page_quality.jsonl', pdf_quality['pages'])
    (output_dir / 'pdf_quality.json').write_text(json.dumps(pdf_quality, ensure_ascii=False, indent=2), encoding='utf-8')
    emit('info', f"PDF text: {pdf_quality['pages_with_text']}/{pdf_quality['total_pages']} pages with text; {pdf_quality['pages_with_warnings']} pages flagged; {len(pdf_quality['chapter_boundaries'])} chapter boundaries")
    emit("stage", "Checking parsed PDF text quality; keyword scope sampling is disabled")
    # Packet overlap must not inflate word/defect counts in the quality report.
    quality_text = ' '.join(region['body'] for region in pdf_inspection['regions']
                            if region['page'] <= pdf_inspection['selected_pages'])
    if not quality_text and packets:
        quality_text = ' '.join(dict.fromkeys(row['text'] for packet in packets for row in packet['sentences']))
    quality = assess_text_quality(quality_text)
    document_scope = {'scope': 'not_screened', 'text_quality': quality['detected_quality'],
                      'quality_signals': quality, 'pdf_quality': pdf_quality, 'sampling_policy': 'all_selected_body_text_without_packet_overlap',
                      'rationale': 'No keyword sampling or document-scope model gate; pathway relevance is assessed with external evidence.'}
    (output_dir / "document_scope.json").write_text(json.dumps(document_scope, ensure_ascii=False, indent=2), encoding="utf-8")
    emit("info", f"Document scope: {document_scope['scope']}; text quality: {document_scope['text_quality']}")
    if document_scope["text_quality"] == "unusable":
        # Persist a terminal summary before raising so the frontend does not
        # later mislabel this intentional quality stop as an unstarted PDF.
        blocked_summary = {
            "pdf": pdf_path.name,
            "pdf_sha256": file_hash(pdf_path),
            "model": settings.extraction_model,
            "extraction_model": settings.extraction_model,
            "validation_model": settings.external_model,
            "external_phase_models": settings.external_phase_models,
            "helper_model": settings.query_model,
            "completed_at": utc_now(),
            "run_status": "blocked_text_quality",
            "schema_version": SCHEMA_VERSION,
            "claims_per_packet": claims_per_packet,
            "llm_calls": llm_call_summary(),
            "task_models": settings.task_models,
            "document_scope": document_scope,
            "extracted": 0,
            "schema_valid": 0,
            "semantic_accepted": 0, "semantic_stage": "removed",
            "source_entailed": 0,
            "externally_evaluated": 0,
            "graph": {"nodes": 0, "edges": 0, "corroborated": 0, "supported": 0},
            "source_graph": {"nodes": 0, "edges": 0, "corroborated": 0, "supported": 0, "source_supported": 0},
            "audit_graph": {"nodes": 0, "edges": 0, "corroborated": 0, "supported": 0},
            "reasoning_paths": 0,
        }
        (output_dir / "summary.json").write_text(json.dumps(blocked_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        raise ValueError("The PDF text is too degraded for faithful extraction. OCR or a cleaner PDF is required.")
    if document_scope["scope"] == "out_of_scope":
        emit("warning", "Document scope screening found no clear pathway evidence; extraction will still inspect each packet conservatively")
    elif document_scope["scope"] == "upstream_only":
        emit("info", "Paper is upstream-only; retained for evidence-backed pathway bridges, not treated as a health-outcome paper")
    finish_stage("document_preparation")
    if resume:
        emit('stage', 'Resuming saved extraction; completed model judgements will be reused only when their input signatures match')
        if checkpoint.get('imported_extraction') and checkpoint.get('claims_per_packet') is None:
            emit('warning', 'Saved extraction reused: its original claim limit per packet was not recorded; no new extraction was requested')
        candidates = read_jsonl(output_dir / 'candidates.jsonl')
        extraction_rejections = read_jsonl(output_dir / 'extraction_rejections.jsonl')
        extraction_rejection_counts = checkpoint.get('rejection_counts', {})
        canonical_duplicates = checkpoint.get('canonical_duplicates', 0)
        packet_results = read_jsonl(output_dir / 'extraction_packets.jsonl')
    else:
        save_checkpoint(output_dir, checkpoint)
        emit("stage", "Extracting evidence-anchored claims from PDF")
        packet_results: list[dict[str, Any]] = []
        save_rows(output_dir / 'extraction_contexts.jsonl', packets)
        try:
            candidates, extraction_rejection_counts, extraction_rejections = extract_claims(
                pdf_path, extractor, max_pages, emit, packets=packets, document_scope=document_scope,
                source_identifiers=source_identifiers, claims_per_packet=claims_per_packet, packet_results=packet_results,
                bibliography=pdf_bibliography(pdf_path, inspection=pdf_inspection),
                extraction_mode=settings.extraction_mode, target_words=settings.extraction_target_words,
            )
        finally:
            # Keep failed and not-started targets visible even when a primary
            # provider error aborts extraction before a checkpoint can complete.
            save_rows(output_dir / 'extraction_packets.jsonl', packet_results)
        candidates, canonical_duplicates = deduplicate_source_claims(candidates)
    completed_packets = sum(row['status'] == 'completed' for row in packet_results)
    failed_packets = [row for row in packet_results if row['status'] != 'completed']
    if failed_packets:
        emit('warning', f"PARTIAL EXTRACTION: {completed_packets}/{len(packet_results)} packets completed. "
             "Missing region claims were not evaluated or rejected; extraction coverage is incomplete.")
    emit("info", f"Merged {canonical_duplicates} same-source wording duplicates before validation")
    finish_stage("extraction")
    save_rows(output_dir / "candidates.jsonl", candidates)
    save_rows(output_dir / "extraction_rejections.jsonl", extraction_rejections)
    checkpoint.update(extraction_complete=True, candidate_ids=[row['candidate_id'] for row in candidates],
                      rejection_counts=extraction_rejection_counts, canonical_duplicates=canonical_duplicates)
    save_checkpoint(output_dir, checkpoint)
    tuples = "\n".join(
        f"<{item['subject']}, {item['relation']}, {item['object']}, {item['subject_type']}, {item['object_type']}>\n"
        f"# evidence: \"{item['source_evidence_span']}\""
        for item in candidates
    )
    (output_dir / "raw_triplets.txt").write_text(tuples + ("\n" if tuples else ""), encoding="utf-8")
    structurally_valid = [item for item in candidates if not item["schema_error"]]
    save_rows(output_dir / "schema_valid_candidates.jsonl",
                [{"candidate_id": item["candidate_id"]} for item in structurally_valid])
    emit("info", f"Structural checks accepted {len(structurally_valid)}/{len(candidates)} candidates; "
         "relations are open and entity labels are provisional")

    semantic_results: list[dict[str, Any]] = []
    # Compatibility ledger only. This is not a model vote or an admission gate.
    for candidate in structurally_valid:
        candidate['semantic_validation'] = {'candidate_id': candidate['candidate_id'], 'decision': 'not_run',
                                            'rationale': 'Standalone semantic gate removed; roles/scope assessed jointly with external evidence.'}
        for role in ('subject', 'object'):
            candidate[f'{role}_normalization'] = normalize_entity(candidate[role], candidate[f'{role}_type'])
    save_rows(output_dir / 'semantic_validation.jsonl', [])
    save_rows(output_dir / 'candidates.jsonl', candidates)
    semantic_accepted = structurally_valid
    save_rows(output_dir / 'semantic_valid_candidates.jsonl', [])
    emit('info', 'Standalone semantic model check disabled; all schema-valid claims proceed to source fidelity')
    emit("stage", f"Checking source evidence for {len(semantic_accepted)} claims")
    saved_source = {row['candidate_id']: row for row in read_jsonl(output_dir / 'source_validation.jsonl')} if resume else {}
    source_results = []
    pending_source = []
    packets_by_number = {packet['packet_number']: packet for packet in packets if 'packet_number' in packet}
    for candidate in semantic_accepted:
        context = section_context(candidate, packets_by_number.get(candidate.get('source_packet_number'), {}))
        if context and candidate.get('source_validation', {}).get('decision') != 'entailed':
            candidate['source_section_context'] = context
        previous = saved_source.get(candidate['candidate_id'], {})
        if (previous.get('input_signature') == claim_stage_signature(candidate, settings.source_model, settings.task_thinking['source'])
                and previous.get('validation_policy') == SOURCE_VALIDATION_POLICY
                and previous.get('decision') in {'entailed', 'unsupported', 'needs_review'} and not previous.get('batch_error')):
            candidate['source_validation'] = previous
            source_results.append(previous)
        else:
            candidate.pop('source_validation', None)
            pending_source.append(candidate)
    save_rows(output_dir / 'source_validation.jsonl', source_results)
    if resume:
        emit('info', f'Reusing {len(source_results)} completed source judgements; {len(pending_source)} claims remain')
    for candidate, source_result in iter_batched_judgments(
            pending_source, task_clients['source'], validate_source_claim, settings.validation_batch_size,
            progress=lambda message: emit("info", message), label="Source"):
        candidate["source_validation"] = source_result
        if source_result.get('applied_name_corrections'):
            emit('info', f"Expanded source-defined names for {candidate['candidate_id']}: "
                 + '; '.join(f"{edit['field']}: {edit['before']} -> {edit['after']}"
                             for edit in source_result['applied_name_corrections']))
        provenance_changes = apply_source_provenance(candidate, source_result)
        if provenance_changes:
            emit('info', f"Moved source occurrence context to provenance for {candidate['candidate_id']}: "
                 + ', '.join(change['field'] for change in provenance_changes))
        source_result['input_signature'] = claim_stage_signature(candidate, settings.source_model, settings.task_thinking['source'])
        source_results.append(source_result)
        save_rows(output_dir / "source_validation.jsonl", source_results)
        save_rows(output_dir / "candidates.jsonl", candidates)
        emit("success" if source_result["decision"] == "entailed" else "warning",
             f"Source status: {source_result['decision']} | {candidate['subject']} -> {candidate['relation']} -> {candidate['object']}")
    finish_stage("source_validation")
    source_entailed = [candidate for candidate in semantic_accepted
                       if candidate["source_validation"]["decision"] == "entailed"]
    save_rows(output_dir / "source_entailed_candidates.jsonl",
                [{"candidate_id": item["candidate_id"]} for item in source_entailed])
    results: list[dict[str, Any]] = read_jsonl(output_dir / 'validation_runs.jsonl') if resume else []
    save_rows(output_dir / "validation_runs.jsonl", results)
    initial_graph = graph_from_candidates(candidates, results, view="main")
    initial_source_graph = graph_from_candidates(candidates, results, view="source")
    initial_audit_graph = graph_from_candidates(candidates, results, view="audit")
    (output_dir / "graph.json").write_text(json.dumps(initial_graph, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "source_graph.json").write_text(json.dumps(initial_source_graph, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "audit_graph.json").write_text(json.dumps(initial_audit_graph, ensure_ascii=False, indent=2), encoding="utf-8")
    selected = source_entailed[:max_claims] if max_claims else source_entailed
    saved_external = {row['candidate_id']: row for row in results}
    complete_external = {candidate['candidate_id'] for candidate in selected
                         if saved_external.get(candidate['candidate_id'], {}).get('validation_signature')
                         == validation_signature(candidate, settings, articles_per_claim, settings.external_top_chunks)
                         and saved_external[candidate['candidate_id']].get('validation_status')
                         not in {None, 'pipeline_error', 'external_in_progress'}}
    selected = [candidate for candidate in selected if candidate['candidate_id'] not in complete_external]
    if resume:
        emit('info', f'Reusing {len(complete_external)} completed external judgements; {len(selected)} claims remain')
    saved_queries = {row['candidate_id']: row for row in read_jsonl(output_dir / 'query_translations.jsonl')} if resume else {}
    query_translations = {}
    for candidate in selected:
        previous = saved_queries.get(candidate['candidate_id'], {})
        if (previous.get('input_signature') == claim_stage_signature(candidate, settings.query_model, settings.task_thinking['query'])
                and not previous.get('result', {}).get('batch_error')):
            query_translations[candidate['candidate_id']] = previous['result']
    def save_query(candidate, result):
        saved_queries[candidate['candidate_id']] = {'candidate_id': candidate['candidate_id'], 'result': result,
            'input_signature': claim_stage_signature(candidate, settings.query_model, settings.task_thinking['query'])}
        save_rows(output_dir / 'query_translations.jsonl', list(saved_queries.values()))
    query_translations.update(translate_queries(
        [candidate for candidate in selected if candidate['candidate_id'] not in query_translations],
        task_clients['query'], settings.query_batch_size, progress=lambda message: emit("info", message),
        on_checkpoint=save_query))
    finish_stage("query_planning")
    run_cache = RunCache()
    emit("stage", f"Validating {len(selected)} claims against Europe PMC and OpenAlex literature")
    nli_validator: NLIValidator | None = None
    if selected and settings.enable_nli:
        emit("info", f"Loading external contradiction checker {settings.nli_model} on {settings.nli_device}")
        try:
            nli_validator = NLIValidator(settings)
            nli_validator.warmup()
            emit("success", "External contradiction checker ready")
        except Exception as exc:
            emit("warning", f"External contradiction checker unavailable: {exc}")
    reranker = SemanticReranker(settings) if selected else None
    if reranker:
        emit("info", f"Loading semantic reranker {settings.embedding_model}")
        try:
            backend = reranker.warmup()
            emit("success", f"Semantic reranker ready ({backend})")
        except Exception as exc:
            emit("error", f"Semantic reranker failed before retrieval: {exc}")
            results.extend({"schema_version": SCHEMA_VERSION, "candidate_id": candidate["candidate_id"],
                            "validation_status": "pipeline_error", "error": str(exc)}
                           for candidate in selected)
            save_rows(output_dir / "validation_runs.jsonl", results)
            selected = []
    emit('info', TaggedMessage(f"Retrieval lookahead: {'enabled' if settings.external_prefetch else 'disabled'}; one retrieval owner; cloud judgements remain sequential", 'literature_search'))
    def prepare_next(candidate):
        check_cancelled()
        return prepare_evidence(candidate, client, reranker, settings, articles_per_claim, settings.external_top_chunks,
            progress=lambda message: emit('info', message), query_translation=query_translations.get(candidate['candidate_id']),
            cache=run_cache, resume_record=saved_external.get(candidate['candidate_id']) if resume else None)
    with prefetch_evidence(selected, prepare_next, settings.external_prefetch) as evidence_iterator:
        for position, (candidate, evidence_future) in enumerate(evidence_iterator, 1):
            emit("info", f"Claim {position}/{len(selected)}: {candidate['subject']} â†’ {candidate['relation']} â†’ {candidate['object']}")
            def save_external_checkpoint(record: dict[str, Any]) -> None:
                record.pop('candidate', None)
                results[:] = [row for row in results if row['candidate_id'] != record['candidate_id']]
                results.append(record)
                save_rows(output_dir / 'validation_runs.jsonl', results)
            try:
                prepared = evidence_future.result()
                result = validate_candidate(candidate, client, reranker, settings, articles_per_claim, settings.external_top_chunks,
                                            progress=lambda message: emit("info", message),
                                            nli_validator=nli_validator,
                                            query_translation=query_translations.get(candidate["candidate_id"]), cache=run_cache,
                                            on_checkpoint=save_external_checkpoint,
                                            resume_record=saved_external.get(candidate['candidate_id']) if resume else None,
                                            prepared_evidence=prepared)
                # candidates.jsonl is the canonical candidate store. Avoid embedding
                # its large source context again in every external-validation row.
                result.pop("candidate", None)
            except LLMServiceError:
                raise
            except Exception as exc:
                result = {"schema_version": SCHEMA_VERSION, "candidate_id": candidate["candidate_id"],
                          "validation_status": "pipeline_error", "error": str(exc)}
                emit("error", f"Claim failed: {exc}")
            save_external_checkpoint(result)
            emit("success", f"Claim status: {result['validation_status']}")
    finish_stage("external_validation")
    assembly = assemble_pathways(ONTOLOGY_DIR, task_clients['pathway_assembly'], settings, emit)
    finish_stage('pathway_assembly')
    emit("stage", "Building knowledge graph")
    graph = graph_from_candidates(candidates, results, view="main")
    source_graph = graph_from_candidates(candidates, results, view="source")
    audit_graph = graph_from_candidates(candidates, results, view="audit")
    (output_dir / "graph.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "source_graph.json").write_text(json.dumps(source_graph, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "audit_graph.json").write_text(json.dumps(audit_graph, ensure_ascii=False, indent=2), encoding="utf-8")
    write_graphml(graph, output_dir / "knowledge_graph.graphml")
    paths = reasoning_paths(graph)
    evaluation_path = write_evaluation_sample(output_dir, candidates, results, preserve_existing_labels=resume)
    finish_stage("graph_building")
    summary = {"pdf": pdf_path.name, "pdf_sha256": file_hash(pdf_path), "model": settings.extraction_model,
               "extraction_model": settings.extraction_model, "validation_model": settings.external_model, "completed_at": utc_now(),
               "external_phase_models": settings.external_phase_models,
               "run_status": "completed_with_warnings" if failed_packets else "completed",
               "extraction_coverage": {"complete": not failed_packets, "total_packets": len(packet_results),
                                       "completed_packets": completed_packets, 'failed_packets': failed_packets,
                                       'unit': 'targets' if settings.extraction_mode == 'target_context' else 'packets',
                                       'exhaustive_claim_recall': False,
                                       'claim_limit_reached_regions': sum(row.get('claim_limit_reached', False) for row in packet_results)},
               "schema_version": SCHEMA_VERSION,
               "resumed": resume,
               "llm_transport": 'api',
               "json_repair_attempts": settings.json_repair_attempts,
               "json_regeneration_attempts": settings.json_regeneration_attempts,
               "response_recovery_policy": "model_edit_or_original_input_regeneration; no_local_corrections",
               "llm_reasoning_policy": {task: model_reasoning_policy(model, settings.task_thinking[task]) for task, model in settings.task_models.items()},
               "claims_per_packet": claims_per_packet,
               "extraction_mode": checkpoint.get('extraction_mode', 'whole_packet'),
               "extraction_target_words": checkpoint.get('extraction_target_words', settings.extraction_target_words),
               "thinking": settings.task_thinking,
               "extraction_generation_budget": checkpoint.get('extraction_generation_budget', settings.extraction_min_output_budget),
               "targeting_version": checkpoint.get('targeting_version'),
               "external_judgement_policy": settings.external_judgement_mode,
               "external_top_k": articles_per_claim,
               "external_top_chunks": settings.external_top_chunks,
               "external_input_max_characters": settings.external_input_max_characters,
               "helper_model": settings.query_model,
               "model_policy": "primary_only",
               "task_models": settings.task_models,
               "models_used": sorted({item['model'] for item in model_requests}),
               "validation_batch_size": settings.validation_batch_size,
               "query_batch_size": settings.query_batch_size,
               "retrieval_workers": settings.retrieval_workers,
               "wall_elapsed_seconds": round(time.monotonic() - run_started, 3),
               "stage_timings_seconds": stage_timings,
               "retrieval_cache": run_cache.stats,
               "embedding_cache_hits": reranker.embedding_cache_hits if reranker else 0,
               "canonical_duplicates_removed": canonical_duplicates,
               "llm_calls": llm_call_summary(),
               "embedding_model": settings.embedding_model,
               "cross_encoder_model": settings.cross_encoder_model,
               "nli_model": settings.nli_model if settings.enable_nli else None,
               "extracted": len(candidates), "schema_valid": len(structurally_valid),
               "semantic_accepted": 0, "semantic_stage": "removed",
               "semantic_type_normalized": sum(item.get("action") == "normalize_types"
                                                and item.get("normalization_applied") is True
                                                for item in semantic_results),
               "semantic_needs_review": sum(item["decision"] == "needs_review" for item in semantic_results),
               "semantic_rejected": sum(item["decision"] == "rejected" for item in semantic_results),
               "source_entailed": len(source_entailed),
               "source_needs_review": sum(item["decision"] == "needs_review" for item in source_results),
               "source_unsupported": sum(item["decision"] == "unsupported" for item in source_results),
               "externally_evaluated": len(results),
               "extraction_rejections": sum(value for key, value in extraction_rejection_counts.items()
                                            if key != "overlap_occurrence_deduplicated"),
               "extraction_overlap_duplicates_removed": extraction_rejection_counts.get("overlap_occurrence_deduplicated", 0),
               "extraction_rejection_counts": extraction_rejection_counts,
               "document_scope": document_scope,
               "graph": graph["counts"],
               "source_graph": source_graph["counts"],
               "audit_graph": audit_graph["counts"],
               "evaluation_sample": evaluation_path.name,
               "reasoning_paths": len(paths)}
    summary['pathway_assembly'] = {'scope':'global', **assembly['counts'],
                                   'model':settings.pathway_assembly_model, 'causal_chain_validated':False}
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "reasoning_paths.json").write_text(json.dumps(paths, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(
        "success",
        f"Finished: {len(candidates)} extracted, {len(results)} externally evaluated, "
        f"{graph['counts']['supported']} independently supported and "
        f"{graph['counts']['corroborated']} corroborated graph claims",
    )
    return summary
