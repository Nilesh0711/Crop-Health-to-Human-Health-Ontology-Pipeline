"""Extract source-anchored claims and retain provenance, citation links and audit records."""

from __future__ import annotations

import json
import re
import time
from backend.shared.cancellation import check_cancelled
from collections import Counter
from pathlib import Path
from typing import Any, Callable
from backend.config.settings import SCHEMA_VERSION
from backend.providers.llm import LLMServiceError, OpenCodeClient
from backend.shared.storage import file_hash, stable_hash
from backend.domain.claim_context import claim_description, local_evidence_context
from backend.stages.claim_extraction.prompt import build_extraction_prompt
from backend.stages.claim_extraction.targeting import build_extraction_targets, targeted_prompt, check_target_evidence
from backend.domain.ontology import schema_error
from backend.domain.modality import uses_conditional_modality
from backend.shared.text import anchored_quote, normalized, source_surface_mention
from backend.stages.claim_extraction.contract import (
    ASSOCIATIVE_TRIGGER_RE,
    COMPARATIVE_CUE_RE,
    CORE_PATHWAY_HINT_RE,
    EXTRACTION_MAX_CLAIMS_PER_PACKET,
    EXTRACTION_OUTPUT_TOKENS,
    EXTRACTION_PATHWAY_TERM_RE,
    EXTRACTION_RELATION_CUE_RE,
    EXTRACTION_RESPONSE_SCHEMA,
    FIGURE_OR_TABLE_RE,
    GENERIC_ENTITIES,
    NON_EMPIRICAL_RE,
    PURPOSE_ONLY_RE,
)
from backend.stages.pdf_parsing.packets import (
    author_year_citation_keys,
    citation_numbers,
    iter_pdf_context_packets,
    link_source_citations,
    pdf_bibliography,
    source_article_identifiers,
    source_paper_title,
)


def extraction_priority_sentence_numbers(sentences: list[dict[str, Any]], limit: int = 14) -> list[int]:
    """Point the extractor toward specific mechanistic sentences in a large packet.

    This is an attention hint, not an acceptance rule. Every emitted claim
    still needs exact anchors and the normal semantic/source validation.
    """
    ranked: list[tuple[int, int]] = []
    for number, sentence in enumerate(sentences, 1):
        text = str(sentence.get("text") or "")
        if PURPOSE_ONLY_RE.search(text) or NON_EMPIRICAL_RE.search(text):
            continue
        relation_cues = len(EXTRACTION_RELATION_CUE_RE.findall(text))
        pathway_terms = len(EXTRACTION_PATHWAY_TERM_RE.findall(text))
        # Domain-lane priority is an attention hint, never a truth/acceptance
        # rule. Explicit nutrient/contaminant/exposure edges precede generic
        # physiology mechanisms with many verbs in the same packet.
        core_lane = bool(CORE_PATHWAY_HINT_RE.search(text))
        score = (100 if core_lane else 0) + relation_cues * 3 + min(pathway_terms, 6)
        if relation_cues and (pathway_terms >= 2 or core_lane):
            ranked.append((score, number))
    return [number for _score, number in sorted(ranked, key=lambda item: (-item[0], item[1]))[:limit]]


def extraction_review_hints(candidate: dict[str, Any], evidence_span: str) -> list[str]:
    """Non-blocking attention hints, never scientific verdicts or local edits."""
    subject = normalized(str(candidate.get("subject") or ""))
    obj = normalized(str(candidate.get("object") or ""))
    evidence = str(evidence_span or "").strip()
    qualifiers = candidate.get("qualifiers") or {}
    hints = []
    if candidate.get('relation') in {'has_contaminant', 'has_nutrient', 'contains'} and candidate.get('subject_type') not in {
            'Crop', 'FoodProduct', 'SoilMaterial', 'SoilCondition', 'SoilChemicalProperty'}:
        hints.append('review_composition_subject_role')
    if subject in GENERIC_ENTITIES or obj in GENERIC_ENTITIES:
        hints.append('review_generic_entity')
    if candidate.get('relation') in {'increases', 'reduces'} and ASSOCIATIVE_TRIGGER_RE.search(
            str(candidate.get('relation_trigger') or '')):
        hints.append('review_association_and_direction')
    if PURPOSE_ONLY_RE.search(evidence):
        hints.append("review_purpose_or_recommendation")
    if FIGURE_OR_TABLE_RE.search(evidence):
        hints.append("review_caption_context")
    if NON_EMPIRICAL_RE.search(evidence):
        hints.append("review_negation_or_absence_of_evidence")
    if COMPARATIVE_CUE_RE.search(evidence):
        if not str(qualifiers.get("comparator") or "").strip() or not str(qualifiers.get("metric") or "").strip():
            hints.append("review_comparator_or_metric")
    if uses_conditional_modality(evidence, candidate.get('relation_trigger', '')) and candidate.get('certainty') != 'conditional':
        hints.append('review_source_modality')
    return hints


def extraction_anchor_error(subject_mention: str, object_mention: str, relation_trigger: str) -> str | None:
    """Missing anchors block admission; semantic anchor concerns are review hints."""
    subject = normalized(subject_mention)
    obj = normalized(object_mention)
    trigger = normalized(relation_trigger)
    if not subject or not obj or not trigger:
        return "missing_source_anchor"
    if subject == obj:
        return "identical_subject_and_object_anchors"
    trigger_pattern = rf"(?<!\w){re.escape(trigger)}(?!\w)"
    if re.search(trigger_pattern, subject) or re.search(trigger_pattern, obj):
        return "entity_anchor_contains_relation_trigger"
    return None


def compact_literal_entity_anchors(candidate: dict[str, Any], subject_mention: str,
                                   object_mention: str, trigger: str, evidence: str
                                   ) -> tuple[str, str, list[dict[str, str]]]:
    """Trim an oversized anchor only when its canonical noun phrase is literal.

    No guessed synonyms, grammar model, or changed claim: the replacement must
    occur inside the original anchor and in the same selected source evidence.
    The normal source judge still owns semantic correctness.
    """
    anchors, adjustments = {"subject": subject_mention, "object": object_mention}, []
    trigger_pattern = rf"(?<!\w){re.escape(normalized(trigger))}(?!\w)"
    for role, mention in list(anchors.items()):
        if not trigger or not re.search(trigger_pattern, normalized(mention)):
            continue
        literal = source_surface_mention(mention, str(candidate.get(role) or ""))
        if (literal and source_surface_mention(evidence, literal)
                and not re.search(trigger_pattern, normalized(literal))):
            anchors[role] = literal
            adjustments.append({"role": role, "original_mention": mention, "mention": literal,
                                "basis": "canonical_phrase_literal_inside_source_anchor"})
    return anchors["subject"], anchors["object"], adjustments


def evidence_span_from_item(item: dict[str, Any], packet_sentences: list[dict[str, Any]]) -> tuple[list[int], list[dict[str, Any]], str] | None:
    """Resolve 1-3 original sentences, marking gaps rather than inventing continuity."""
    numbers = item.get("evidence_sentence_numbers")
    if isinstance(numbers, list) and 1 <= len(numbers) <= 3 and not any(isinstance(number, bool) or not isinstance(number, int) for number in numbers):
        indexes = sorted(set(numbers))
        if numbers == indexes and 1 <= indexes[0] <= indexes[-1] <= len(packet_sentences):
            evidence_sentences = [packet_sentences[index - 1] for index in indexes]
            span = evidence_sentences[0]['text']
            for previous, number, sentence in zip(indexes, indexes[1:], evidence_sentences[1:]):
                span += (' ' if number == previous + 1 else '\n[Non-adjacent source fragment]\n') + sentence['text']
            return indexes, evidence_sentences, span

    # Preserve exact provenance if a model returns a valid quote but omits the
    # requested sentence number. Only a quote contained in one packet sentence is
    # accepted as a compatibility fallback.
    quote = str(item.get("quote", "")).strip()
    for index, sentence in enumerate(packet_sentences, 1):
        if anchored_quote(sentence["text"], quote):
            return [index], [sentence], sentence["text"]
    return None


def extract_claims(pdf_path: Path, client: OpenCodeClient, max_pages: int,
                   emit: Callable[[str, str], None], packets: list[dict[str, Any]] | None = None,
                   document_scope: dict[str, Any] | None = None,
                   source_identifiers: dict[str, str] | None = None,
                   claims_per_packet: int = EXTRACTION_MAX_CLAIMS_PER_PACKET,
                   packet_results: list[dict[str, Any]] | None = None,
                   repair_target: dict[str, Any] | None = None, bibliography=None,
                   extraction_mode: str = 'whole_packet', target_words: int = 800
                   ) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    digest = file_hash(pdf_path)
    document_id = "local-" + stable_hash(f"{pdf_path.stem}|{digest}", 32)
    source_title = source_paper_title(pdf_path)
    source_identifiers = source_identifiers or source_article_identifiers(pdf_path)
    if extraction_mode not in {'whole_packet', 'target_context'}:
        raise ValueError('Unknown extraction mode')
    # A claim repair already has an explicit single-claim target. Retain its
    # original packet IDs rather than scheduling unrelated new regions.
    if repair_target is not None:
        extraction_mode = 'whole_packet'
    system = build_extraction_prompt(claims_per_packet, extraction_mode)
    if repair_target is not None:
        system += ("\nREPAIR ONE EXISTING CLAIM. Return at most one correction of the specified claim, "
                   "using only this PDF context and exact sentence IDs. Use the rejection reason to inspect "
                   "direction, referents, modality and qualifiers again. Do not choose an unrelated claim, "
                   "invent a missing relation, or weaken restrictions just to pass. Preserve the original "
                   "exposure/outcome question where supported; return an empty claims array if no faithful "
                   "correction is possible. The previous verdict is feedback, not proof. REPAIR TARGET: "
                   + json.dumps(repair_target, ensure_ascii=False))
    candidates: list[dict[str, Any]] = []
    seen_occurrences: dict[tuple[Any, ...], dict[str, Any]] = {}
    rejected: Counter[str] = Counter()
    rejection_records: list[dict[str, Any]] = []
    bibliography = pdf_bibliography(pdf_path) if bibliography is None else bibliography
    response_schema = json.loads(json.dumps(EXTRACTION_RESPONSE_SCHEMA))
    if claims_per_packet:
        response_schema["properties"]["claims"]["maxItems"] = claims_per_packet
    packets = packets if packets is not None else list(iter_pdf_context_packets(pdf_path, max_pages))
    if not packets:
        raise ValueError("The PDF has no extractable text. OCR is required before extraction.")
    if extraction_mode == 'target_context':
        packets = build_extraction_targets(packets, target_words)
        emit('info', f"Target/context extraction: {len(packets)} targets; about {target_words} words per target; "
             "original packets retained as reading context; no LLM discovery pass")
    packet_results = packet_results if packet_results is not None else []
    planned_records = []
    for packet in packets:
        record = {'packet_number': packet['packet_number'], 'section_heading': packet['section_heading'],
                  'page_start': packet['page_start'], 'page_end': packet['page_end'],
                  'word_count': packet['word_count'], 'extraction_mode': extraction_mode, 'status': 'not_started'}
        if extraction_mode == 'target_context':
            record.update({key: packet[key] for key in (
                'target_id', 'target_number', 'target_sentence_numbers', 'target_source_sentence_ids',
                'target_word_count', 'target_page_start', 'target_page_end', 'targeting_version')})
        planned_records.append(record)
    packet_results.extend(planned_records)
    for index, (packet, packet_record) in enumerate(zip(packets, planned_records), 1):
        check_cancelled()
        packet_started = time.monotonic()
        packet_record['status'] = 'started'
        packet_sentences = packet["sentences"]
        # A heading once per section preserves exactly the same source text and
        # IDs without repeating a long heading on every sentence.
        packet_lines, last_heading = [], None
        for number, sentence in enumerate(packet_sentences, 1):
            heading = sentence.get("section_heading", packet["section_heading"])
            if heading != last_heading:
                packet_lines.append(f"[Section: {heading}]")
                last_heading = heading
            packet_lines.append(f"[{number}] {sentence['text']}")
        packet_text = "\n".join(packet_lines)
        priority_numbers = (extraction_priority_sentence_numbers(packet_sentences)
                            if extraction_mode == 'whole_packet' else [])
        # A generative model can underweight a bare list of sentence numbers in a long
        # prompt and then spend the limited claim slots on the first broad
        # statements it sees. Repeat only the ranked evidence candidates as a
        # compact attention index. They keep their original sentence numbers,
        # so this does not create a second evidence source or weaken the
        # exact-span provenance checks below.
        priority_index = "\n".join(
            f"[{number}] [Section: {packet_sentences[number - 1].get('section_heading', packet['section_heading'])}] "
            f"{packet_sentences[number - 1]['text']}"
            for number in priority_numbers
        )
        label = (f"extraction target {index}/{len(packets)} ({packet['target_id']}; packet {packet['packet_number']})"
                 if extraction_mode == 'target_context' else f"extraction packet {index}/{len(packets)}")
        emit("info", f"Extracting {packet['section_heading']} (pages {packet['page_start']}-{packet['page_end']}), "
             f"{label}; {packet.get('target_word_count', packet['word_count'])} extraction words; "
             f"~{packet['estimated_token_count']} source input tokens")
        def check_extraction_complete(response):
            from backend.providers.structured_output import IncompleteResponseError
            for row in response['claims']:
                required = ('claim_text', 'subject', 'relation', 'object', 'subject_source_mention', 'object_source_mention', 'relation_trigger')
                if any(not str(row.get(field) or '').strip() for field in required):
                    raise IncompleteResponseError('An extracted claim omitted required endpoint or source wording')
            check_target_evidence(response, packet['target_sentence_numbers'] if extraction_mode == 'target_context'
                                  else range(1, len(packet_sentences) + 1))
        try:
            user = (targeted_prompt(packet, source_title) if extraction_mode == 'target_context' else
                                      f"Source paper: {source_title}\n"
                                      f"Section: {packet['section_heading']}\n"
                                      f"Pages: {packet['page_start']}-{packet['page_end']}\n"
                                      "PRIORITY EVIDENCE INDEX (ranked; prefer the most specific eligible claims here):\n"
                                      f"{priority_index or '(no high-value candidate detected)'}\n"
                                      f"SECTION CONTEXT PACKET:\n{packet_text}")
            answer_budget = (EXTRACTION_OUTPUT_TOKENS if claims_per_packet else
                             min(12000, max(4000, packet.get('target_word_count', packet['word_count']) * 8)))
            packet_schema = json.loads(json.dumps(response_schema))
            packet_schema['properties']['claims']['items']['properties']['evidence_sentence_numbers']['items']['enum'] = (
                packet['target_sentence_numbers'] if extraction_mode == 'target_context' else list(range(1, len(packet_sentences) + 1)))
            result = client.chat_json(system, user, max_tokens=answer_budget,
                                      json_schema=packet_schema, request_label=label,
                                      response_check=check_extraction_complete)
        except LLMServiceError as exc:
            packet_record.update(status='service_error', error=str(exc), elapsed_seconds=round(time.monotonic() - packet_started, 3))
            emit('error', f"{label} failed; extraction is incomplete; "
                 f"{len(packets) - index} extraction regions were not processed")
            raise
        except Exception as exc:
            packet_record.update(status='timeout' if 'timed out' in str(exc).casefold() else 'failed',
                                 error=str(exc)[:650], elapsed_seconds=round(time.monotonic() - packet_started, 3))
            emit("warning", f"Extraction response failed for {packet['section_heading']}: {exc}")
            continue
        extracted_items = result.get("claims")
        if not isinstance(extracted_items, list):
            packet_record.update(status='failed', error='Model returned a non-list claims field',
                                 elapsed_seconds=round(time.monotonic() - packet_started, 3))
            emit("warning", f"{packet['section_heading']}: model returned a non-list claims field")
            continue
        packet_record.update(status='completed', proposed_claims=len(extracted_items),
                             claim_limit_reached=bool(claims_per_packet and len(extracted_items) >= claims_per_packet),
                             elapsed_seconds=round(time.monotonic() - packet_started, 3))
        for item in extracted_items:
            if not isinstance(item, dict):
                rejected["malformed"] += 1
                rejection_records.append({"reason": "malformed", "packet_number": packet["packet_number"],
                                          "section_heading": packet["section_heading"], "proposed_claim": {}})
                continue
            subject = str(item.get("subject", "")).strip()
            proposed_subject_mention = str(item.get("subject_source_mention", "")).strip()
            relation = str(item.get("relation", "")).strip().lower()
            obj = str(item.get("object", "")).strip()
            proposed_object_mention = str(item.get("object_source_mention", "")).strip()
            proposed_relation_trigger = str(item.get("relation_trigger", "")).strip()
            candidate = {"subject": subject, "relation": relation, "object": obj,
                         "source_context": str(item.get("source_context") or "").strip()[:300],
                         "subject_type": str(item.get("subject_type", "")).strip(),
                         "object_type": str(item.get("object_type", "")).strip(),
                         "certainty": str(item.get("certainty", "")).strip().lower(),
                         "relation_trigger": proposed_relation_trigger}
            raw_qualifiers = item.get("qualifiers")
            candidate["qualifiers"] = {
                key: str(raw_qualifiers.get(key, "")).strip()[:160]
                for key in ("crop_or_species", "population", "condition", "comparator", "metric")
            } if isinstance(raw_qualifiers, dict) else {}
            evidence = evidence_span_from_item(item, packet_sentences)
            if not subject or not relation or not obj or evidence is None:
                rejected["malformed"] += 1
                rejection_records.append({"reason": "malformed", "packet_number": packet["packet_number"],
                                          "section_heading": packet["section_heading"],
                                          "proposed_source_anchors": {"subject": proposed_subject_mention,
                                                                     "object": proposed_object_mention,
                                                                     "relation": proposed_relation_trigger},
                                          "proposed_claim_text": item.get("claim_text"),
                                          "proposed_claim": candidate})
                continue
            evidence_numbers, evidence_sentences, evidence_span = evidence
            evidence_details = {
                'source_evidence_is_contiguous': evidence_numbers == list(range(evidence_numbers[0], evidence_numbers[-1] + 1)),
                'source_evidence_fragments': [{'sentence_number': n, 'page': s['page'], 'text': s['text']}
                                               for n, s in zip(evidence_numbers, evidence_sentences)],
                'source_intervening_context': [{'sentence_number': n, 'page': packet_sentences[n-1]['page'],
                                               'text': packet_sentences[n-1]['text']}
                                              for n in range(evidence_numbers[0], evidence_numbers[-1]+1) if n not in evidence_numbers],
            }
            # Defence in depth for alternate clients that do not invoke the
            # provider response_check callback. Never accept context-only proof.
            if extraction_mode == 'target_context' and not set(evidence_numbers) <= set(packet['target_sentence_numbers']):
                rejected['outside_extraction_target'] += 1
                rejection_records.append({'reason': 'outside_extraction_target', 'target_id': packet['target_id'],
                                          'packet_number': packet['packet_number'], 'proposed_claim': candidate,
                                          'source_evidence_sentence_numbers': evidence_numbers})
                continue
            evidence_heading = str(evidence_sentences[0].get("section_heading") or packet["section_heading"])

            def record_rejection(reason: str) -> None:
                rejection_records.append({
                    "reason": reason,
                    "packet_number": packet["packet_number"],
                    "section_heading": packet["section_heading"],
                    "proposed_claim": dict(candidate),
                    "proposed_source_anchors": {"subject": proposed_subject_mention,
                                                "object": proposed_object_mention,
                                                "relation": proposed_relation_trigger},
                    "proposed_claim_text": item.get("claim_text"),
                    "source_evidence_sentence_numbers": evidence_numbers,
                    "source_evidence_span": evidence_span,
                })

            review_hints = extraction_review_hints(candidate, evidence_span)
            subject_source_mention = source_surface_mention(evidence_span, proposed_subject_mention)
            object_source_mention = source_surface_mention(evidence_span, proposed_object_mention)
            relation_trigger = source_surface_mention(evidence_span, proposed_relation_trigger)
            if not subject_source_mention or not object_source_mention or not relation_trigger:
                rejected["unanchored_evidence"] += 1
                record_rejection("unanchored_evidence")
                continue
            subject_source_mention, object_source_mention, anchor_adjustments = compact_literal_entity_anchors(
                candidate, subject_source_mention, object_source_mention, relation_trigger, evidence_span)
            anchor_error = extraction_anchor_error(subject_source_mention, object_source_mention, relation_trigger)
            if anchor_error == 'missing_source_anchor':
                rejected[anchor_error] += 1
                record_rejection(anchor_error)
                continue
            if anchor_error:
                review_hints.append('review_' + anchor_error)
            extraction_normalizations: list[dict[str, str]] = []
            qualifier_adjustments: list[dict[str, str]] = []
            evidence_page = evidence_sentences[0]["page"]
            cited_numbers = citation_numbers(evidence_span)
            cited_keys = author_year_citation_keys(evidence_span)
            cited_references, citation_links = link_source_citations(bibliography, evidence_span, evidence_page, packet.get('chapter_id', ''))
            for link in citation_links:
                if link['status'] == 'missing_bibliography_link':
                    emit('warning', f"Citation {link['citation_key']} on PDF page {evidence_page} has no unambiguous chapter bibliography link")
            contextual_entity_normalization = None
            evidence_record = {
                **evidence_details,
                "page": evidence_page,
                "page_start": packet["page_start"],
                "page_end": packet["page_end"],
                "section_heading": evidence_heading,
                "sentence_numbers": evidence_numbers,
                "evidence_span": evidence_span,
                "subject_source_mention": subject_source_mention,
                "object_source_mention": object_source_mention,
                "relation_trigger": relation_trigger,
                "anchor_adjustments": anchor_adjustments,
                "certainty": candidate["certainty"],
                "subject_type": candidate["subject_type"],
                "object_type": candidate["object_type"],
                "qualifiers": dict(candidate["qualifiers"]),
                "source_context": candidate.get("source_context", ""),
                "citation_numbers": cited_numbers,
                "citation_keys": cited_keys,
                "references": cited_references,
                "citation_links": citation_links,
            }
            # Only the same assertion and restrictions from the same evidence
            # are deduplicated. Differing qualifiers must reach assessment.
            key = (normalized(subject), relation, normalized(obj), candidate["certainty"],
                   candidate["subject_type"], candidate["object_type"], evidence_page,
                   normalized(evidence_span), json.dumps(candidate['qualifiers'], sort_keys=True),
                   normalized(candidate.get('source_context', '')))
            if key in seen_occurrences:
                existing = seen_occurrences[key]
                existing.setdefault("source_evidence_records", []).append(evidence_record)
                existing["source_citation_numbers"] = sorted(set(existing.get("source_citation_numbers", [])) | set(cited_numbers))
                existing["source_citation_keys"] = sorted(set(existing.get("source_citation_keys", [])) | set(cited_keys))
                known = {str(item.get("citation_key") or item.get("reference_number"))
                         for item in existing.get("source_references", [])}
                existing.setdefault("source_citation_links", []).extend(link for link in citation_links if link not in existing.get("source_citation_links", []))
                existing.setdefault("source_references", []).extend(
                    item for item in cited_references
                    if str(item.get("citation_key") or item.get("reference_number")) not in known
                )
                rejected["overlap_occurrence_deduplicated"] += 1
                record_rejection("overlap_occurrence_deduplicated")
                continue
            candidate_id = "candidate-" + stable_hash(
                f"{document_id}|{normalized(subject)}|{relation}|{normalized(obj)}|"
                f"{candidate['certainty']}|{candidate['subject_type']}|{candidate['object_type']}|"
                f"{json.dumps(candidate['qualifiers'], sort_keys=True)}|{evidence_page}|{normalized(evidence_span)}"
                + (f"|context:{normalized(candidate['source_context'])}" if normalized(candidate.get('source_context', '')) else ''), 32)
            candidate.update({**evidence_details, "schema_version": SCHEMA_VERSION, "candidate_id": candidate_id,
                              "source_document_id": document_id, "source_paper_title": source_title,
                               "source_doi": source_identifiers.get("source_doi", ""),
                               "source_pmcid": source_identifiers.get("source_pmcid", ""),
                               "source_pmid": source_identifiers.get("source_pmid", ""),
                              "source_pdf_sha256": digest, "source_page": evidence_page,
                              "source_page_start": packet["page_start"], "source_page_end": packet["page_end"],
                              "source_section_heading": evidence_heading,
                              "source_evidence_section_heading": evidence_heading,
                              "source_packet_number": packet["packet_number"],
                              "source_context_estimated_tokens": packet["estimated_token_count"],
                              "source_evidence_sentence_numbers": evidence_numbers,
                              "source_evidence_span": evidence_span,
                              "subject_source_mention": subject_source_mention,
                              "object_source_mention": object_source_mention,
                              "relation_trigger": relation_trigger,
                              "anchor_adjustments": anchor_adjustments,
                              "source_citation_numbers": cited_numbers,
                              "source_citation_keys": cited_keys,
                              "source_references": cited_references,
                              "source_chapter_id": packet.get("chapter_id"),
                              "source_citation_links": citation_links,
                              "source_evidence_records": [evidence_record],
                              "qualifier_adjustments": qualifier_adjustments,
                              "extraction_review_hints": review_hints,
                              "source_context": candidate.get("source_context", ""),
                              "contextual_entity_normalization": contextual_entity_normalization,
                              "extraction_normalizations": extraction_normalizations,
                              "document_scope": (document_scope or {}).get("scope", "uncertain"),
                              "source_context_packet": " ".join(sentence["text"] for sentence in packet_sentences),
                              "source_local_context": local_evidence_context(packet_sentences, evidence_numbers),
                              "extraction_type_variants": [{"subject_type": candidate["subject_type"],
                                                            "object_type": candidate["object_type"]}],
                              })
            candidate["claim_text"], candidate["claim_text_origin"] = claim_description({**candidate, "claim_text": item.get("claim_text")})
            if extraction_mode == 'target_context':
                candidate.update(source_extraction_mode=extraction_mode, source_target_id=packet['target_id'],
                                 source_target_sentence_numbers=packet['target_sentence_numbers'],
                                 source_target_sentence_ids=packet['target_source_sentence_ids'])
            candidate["schema_error"] = schema_error(candidate)
            candidates.append(candidate)
            seen_occurrences[key] = candidate
        emit("success", f"{packet['section_heading']}: {len(candidates)} accepted candidates so far")
    emit("info", f"Extraction complete: {len(candidates)} unique candidates; "
         f"{rejected.get('overlap_occurrence_deduplicated', 0)} overlap duplicates removed; "
         f"{sum(value for key, value in rejected.items() if key != 'overlap_occurrence_deduplicated')} discarded ({dict(rejected)})")
    return candidates, dict(rejected), rejection_records
