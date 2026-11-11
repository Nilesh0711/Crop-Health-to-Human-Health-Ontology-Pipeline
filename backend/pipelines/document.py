"""Coordinate the PDF stages, checkpoints, model metrics and final run artifacts."""

from __future__ import annotations

import json
import time
import threading
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable
from pypdf import PdfReader
from backend.config.settings import ONTOLOGY_DIR, SCHEMA_VERSION, APPLICATION_VERSION, Settings
from backend.config.pipeline import PIPELINE_MODE, PAUSED_MESSAGE, POST_GROUNDING_STAGES_PAUSED, active_task_models
from backend.config.pipeline import ONTOLOGY_GROUNDING_PAUSED, GROUNDING_PAUSED_MESSAGE
from backend.pipelines.unvalidated import publish_unvalidated_views, paused_metadata
from backend.pipelines.checkpoints import resume_metadata, save_checkpoint
from backend.shared.cancellation import check_cancelled
from backend.shared.activity import TaggedMessage
from backend.providers.llm import (
    OpenCodeClient,
    format_request_metrics,
    model_reasoning_policy,
    request_log_level,
)
from backend.shared.storage import file_hash, utc_now, read_jsonl
from backend.shared.artifacts import save_rows
from backend.stages.claim_extraction.deduplication import deduplicate_source_claims
from backend.stages.evaluation.metrics import write_evaluation_sample
from backend.stages.pdf_parsing.quality import assess_text_quality
from backend.stages.pdf_parsing.structure import inspect_pdf
from backend.stages.pdf_parsing.layout import PARSING_POLICY, LEGACY_PARSING_POLICY
from backend.stages.claim_extraction.extractor import extract_claims, refresh_candidate_citations
from backend.stages.claim_extraction.targeting import TARGETING_VERSION
from backend.stages.claim_extraction.contract import EXTRACTION_CONTRACT_VERSION
from backend.pipelines.ontology import run_grounding
from backend.ontology.catalog import catalog as load_catalog
from backend.stages.pdf_parsing.packets import (
    iter_pdf_context_packets,
    pdf_bibliography,
    source_article_identifiers,
)


def run_pipeline(pdf_path: Path, max_pages: int, articles_per_pathway: int,
                 emit: Callable[[str, str], None], claims_per_packet: int | None = None,
                 resume: bool = False, extraction_mode: str | None = None,
                 extraction_target_words: int | None = None,
                 extraction_generation_budget: int | None = None,
                 thinking: dict[str, bool] | None = None,
                 neighbor_max_pairs: int | None = None,
                 external_assessment_id: str | None = None) -> dict[str, Any]:
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
    reference = load_catalog()
    mode = PIPELINE_MODE
    settings = replace(settings,
                       external_top_k=articles_per_pathway,
                       neighbor_max_pairs=settings.neighbor_max_pairs if neighbor_max_pairs is None else neighbor_max_pairs)
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
        'extraction_generation_budget': settings.extraction_min_output_budget, 'targeting_version': TARGETING_VERSION,
        'parsing_policy':PARSING_POLICY}
    scope_signature = reference.profile['id'] + '|' + reference.profile['research_question']
    if resume and checkpoint.get('extraction_scope') not in (None, scope_signature):
        raise ValueError('The crop/research question changed; start a fresh extraction for the new scope')
    checkpoint.setdefault('extraction_scope', scope_signature if not resume else None)
    checkpoint['ontology_reference_signature'] = reference.signature
    checkpoint['extraction_contract_version'] = EXTRACTION_CONTRACT_VERSION if not resume else checkpoint.get('extraction_contract_version')
    checkpoint['thinking'] = settings.task_thinking
    checkpoint.update(paused_metadata())
    checkpoint['knowledge_building_mode'] = mode
    checkpoint['neighbor_max_pairs'] = settings.neighbor_max_pairs
    if external_assessment_id is not None:
        raise ValueError('External validation is temporarily archived; resume extraction and assembly instead')
    active_models = active_task_models(settings)
    emit('info', TaggedMessage(PAUSED_MESSAGE, 'system'))
    available_models = OpenCodeClient(settings).health_check().get("models", [])
    for task, model in active_models.items():
        if model.split('#', 1)[0] not in available_models:
            raise RuntimeError(f"No configured OpenCode model is available for {task}: {model}")
    def log_model_request(metrics: dict[str, Any]) -> None:
        with request_log_lock:
            model_requests.append({"at": utc_now(), **metrics})
            save_rows(output_dir / "llm_requests.jsonl", model_requests)
        emit(request_log_level(metrics), TaggedMessage(format_request_metrics(metrics), metrics.get("stage") or "system"))

    task_clients = {task: OpenCodeClient(
        replace(settings, request_timeout_seconds=settings.extraction_timeout_seconds) if task == 'extraction' else settings,
        model, task=task, on_request=log_model_request, on_progress=lambda event: emit("progress", json.dumps(event)),
        response_dir=output_dir / 'model_responses') for task, model in active_models.items()}
    extractor = task_clients['extraction']
    def llm_call_summary() -> dict[str, Any]:
        counters = {task: task_client.request_metrics for task, task_client in task_clients.items()}
        return {**counters, 'validation': {'requests': 0, 'elapsed_seconds': 0},
                'helper': {'requests': 0, 'elapsed_seconds': 0},
                'total_requests': sum(item['requests'] for item in counters.values())}
    for task, model in active_models.items():
        emit('info', f"LLM {task}: {model}; Thinking {'on' if settings.task_thinking[task] else 'off'}")
    emit('info', f"Extraction mode: {settings.extraction_mode}; claim limit per extraction region: {claims_per_packet}; "
         f"initial generation budget (reasoning + answer): {settings.extraction_min_output_budget} tokens")
    parsing_policy = checkpoint.get('parsing_policy',LEGACY_PARSING_POLICY) if resume else PARSING_POLICY
    pdf_inspection = inspect_pdf(PdfReader(str(pdf_path)), max_pages, parsing_policy)
    packets = list(iter_pdf_context_packets(pdf_path, max_pages, inspection=pdf_inspection))
    if resume and (saved_packets := read_jsonl(output_dir / 'extraction_contexts.jsonl')):
        packets = saved_packets  # Never renumber saved evidence using a different parser.
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
                      'rationale': ('No document-scope model gate. Extraction is guided by the potato-nutrition profile. Ontology grounding and later assessments are paused.'
                                    if ONTOLOGY_GROUNDING_PAUSED else 'No document-scope model gate. Source fidelity and external validation are paused. Potato-nutrition application scope is assessed during ontology grounding.')}
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
            **paused_metadata(),
            "completed_at": utc_now(),
            "run_status": "blocked_text_quality",
            "schema_version": SCHEMA_VERSION,
            "application_version": APPLICATION_VERSION,
            "claims_per_packet": claims_per_packet,
            "llm_calls": llm_call_summary(),
            "task_models": active_models,
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
    finish_stage("document_preparation")
    if resume and checkpoint.get('extraction_complete') and (output_dir / 'candidates.jsonl').is_file():
        emit('stage', 'Resuming saved extraction; completed model judgements will be reused only when their input signatures match')
        if checkpoint.get('imported_extraction') and checkpoint.get('claims_per_packet') is None:
            emit('warning', 'Saved extraction reused: its original claim limit per packet was not recorded; no new extraction was requested')
        candidates = read_jsonl(output_dir / 'candidates.jsonl')
        extraction_rejections = read_jsonl(output_dir / 'extraction_rejections.jsonl')
        extraction_rejection_counts = checkpoint.get('rejection_counts', {})
        canonical_duplicates = checkpoint.get('canonical_duplicates', 0)
        packet_results = read_jsonl(output_dir / 'extraction_packets.jsonl')
        bib = pdf_bibliography(pdf_path, inspection=pdf_inspection)
        candidates = refresh_candidate_citations(candidates, bib, emit)
        save_rows(output_dir / 'candidates.jsonl', candidates)
    else:
        save_checkpoint(output_dir, checkpoint)
        emit("stage", "Resuming extraction of evidence-anchored claims from PDF" if resume else "Extracting evidence-anchored claims from PDF")
        packet_results: list[dict[str, Any]] = []
        save_rows(output_dir / 'extraction_contexts.jsonl', packets)
        try:
            candidates, extraction_rejection_counts, extraction_rejections = extract_claims(
                pdf_path, extractor, max_pages, emit, packets=packets, document_scope=document_scope,
                source_identifiers=source_identifiers, claims_per_packet=claims_per_packet, packet_results=packet_results,
                bibliography=pdf_bibliography(pdf_path, inspection=pdf_inspection),
                scope_instructions=reference.extraction_instructions(),
                extraction_mode=settings.extraction_mode, target_words=settings.extraction_target_words,
                resume_cache_dir=output_dir if resume else None,
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
        f"<{item['subject']}, {item['relation']}, {item['object']}>\n"
        f"# evidence: \"{item['source_evidence_span']}\""
        for item in candidates
    )
    (output_dir / "raw_triplets.txt").write_text(tuples + ("\n" if tuples else ""), encoding="utf-8")
    structurally_valid = [item for item in candidates if not item["schema_error"]]
    save_rows(output_dir / "schema_valid_candidates.jsonl",
                [{"candidate_id": item["candidate_id"]} for item in structurally_valid])
    emit("info", f"Structural checks accepted {len(structurally_valid)}/{len(candidates)} candidates; "
         + ("ontology grounding is paused and classifications are not assessed" if ONTOLOGY_GROUNDING_PAUSED
            else "ontology classification is deferred to grounding"))

    if ONTOLOGY_GROUNDING_PAUSED:
        emit('info', GROUNDING_PAUSED_MESSAGE)
        provisional = {'counts': {'grounded_mentions': 0, 'mapped_claims': 0}}
        grounding_summary = {'status': 'not_run', 'assessed': False, 'claims': 0,
            'mapped_claims': 0, 'not_assessed': len(candidates)}
    else:
        provisional, grounding_summary = run_grounding(ONTOLOGY_DIR, output_dir, structurally_valid,
            task_clients['pathway_assembly'], settings, emit, reference=reference)
        finish_stage('ontology_grounding' if POST_GROUNDING_STAGES_PAUSED else 'ontology_grounding_and_reasoning')
    graph = publish_unvalidated_views(output_dir, provisional)
    evaluation_path = None
    if not POST_GROUNDING_STAGES_PAUSED:
        evaluation_path = write_evaluation_sample(output_dir, candidates, [], preserve_existing_labels=resume,
                                                  validation_paused=True)
        finish_stage('graph_building')
    summary = {
        'pdf': pdf_path.name, 'pdf_sha256': file_hash(pdf_path),
        'completed_at': utc_now(), 'schema_version': SCHEMA_VERSION,
        'application_version': APPLICATION_VERSION,
        'extraction_contract_version': EXTRACTION_CONTRACT_VERSION,
        'run_status': 'completed_with_warnings' if failed_packets else 'completed',
        **paused_metadata(), 'resumed': resume,
        'extracted': len(candidates), 'schema_valid': len(structurally_valid),
        'source_entailed': 0, 'source_unsupported': 0, 'source_needs_review': 0,
        'source_not_assessed': len(candidates),
        'source_project_scope': {'in_scope': 0, 'out_of_scope': 0,
                                 'needs_review': 0, 'not_assessed': len(candidates)},
        'pathway_eligible_claims': grounding_summary['mapped_claims'],
        'ontology_grounding': grounding_summary,
        'semantic_stage': 'removed', 'semantic_accepted': 0,
        'extraction_model': settings.extraction_model, 'model': settings.extraction_model,
        'task_models': active_models, 'model_policy': 'primary_only',
        'models_used': sorted({item['model'] for item in model_requests}),
        'llm_calls': llm_call_summary(), 'thinking': {task: settings.task_thinking[task] for task in active_models},
        'llm_reasoning_policy': {task: model_reasoning_policy(model, settings.task_thinking[task])
                                 for task, model in active_models.items()},
        'llm_transport': 'api', 'claims_per_packet': claims_per_packet,
        'extraction_mode': settings.extraction_mode,
        'extraction_target_words': settings.extraction_target_words,
        'extraction_generation_budget': settings.extraction_min_output_budget,
        'targeting_version': checkpoint.get('targeting_version'),
        'extraction_coverage': {'complete': not failed_packets, 'total_packets': len(packet_results),
                                'completed_packets': completed_packets, 'failed_packets': failed_packets,
                                'unit': 'targets' if settings.extraction_mode == 'target_context' else 'packets',
                                'exhaustive_claim_recall': False,
                                'claim_limit_reached_regions': sum(row.get('claim_limit_reached', False) for row in packet_results)},
        'extraction_rejections': sum(value for key, value in extraction_rejection_counts.items()
                                    if key != 'overlap_occurrence_deduplicated'),
        'extraction_rejection_counts': extraction_rejection_counts,
        'extraction_overlap_duplicates_removed': extraction_rejection_counts.get('overlap_occurrence_deduplicated', 0),
        'canonical_duplicates_removed': canonical_duplicates,
        'document_scope': document_scope, 'graph': graph['counts'],
        'pathway_assembly': {'scope': 'global', **provisional['counts'],
                             'model': settings.pathway_assembly_model, 'causal_chain_validated': False,
                             'validation_status': 'not_assessed',
                             'status': 'paused' if POST_GROUNDING_STAGES_PAUSED else 'completed'},
        'stage_timings_seconds': stage_timings,
        'wall_elapsed_seconds': round(time.monotonic() - run_started, 3),
        'evaluation_sample': evaluation_path.name if evaluation_path else None, 'reasoning_paths': 0,
    }
    (output_dir / 'summary.json').write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding='utf-8')
    emit('success', (f"Finished: {len(candidates)} extracted claims. Use Run grounded claims for manual ontology grounding. Later stages remain paused."
        if ONTOLOGY_GROUNDING_PAUSED else f"Finished: {len(candidates)} extracted claims; {provisional['counts']['grounded_mentions']} grounded endpoint mentions. Scientific validation was not run."))
    return summary
