"""Extract source-anchored claims and retain provenance, citation links and audit records."""

from __future__ import annotations

import json
import re
import time
import concurrent.futures
from backend.shared.cancellation import check_cancelled
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from backend.config.settings import SCHEMA_VERSION
from backend.providers.llm import LLMServiceError, OpenCodeClient
from backend.shared.storage import file_hash, stable_hash
from backend.domain.claim_context import claim_description, local_evidence_context
from backend.stages.claim_extraction.prompt import build_extraction_prompt
from backend.stages.claim_extraction.targeting import build_extraction_targets, targeted_prompt, check_target_evidence
from backend.domain.ontology import schema_error, CORE_DOMAIN_TYPES
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


@dataclass(frozen=True)
class _DocumentEvidence:
    document_id: str
    digest: str
    source_title: str
    source_identifiers: dict[str, str]
    document_scope: dict[str, Any] | None
    bibliography: Any
    extraction_mode: str


@dataclass
class _ExtractionResults:
    candidates: list[dict[str, Any]] = field(default_factory=list)
    seen_occurrences: dict[tuple[Any, ...], dict[str, Any]] = field(default_factory=dict)
    rejected: Counter[str] = field(default_factory=Counter)
    rejection_records: list[dict[str, Any]] = field(default_factory=list)


def _packet_coverage_record(packet: dict[str, Any], extraction_mode: str) -> dict[str, Any]:
    record = {'packet_number': packet['packet_number'], 'section_heading': packet['section_heading'],
              'page_start': packet['page_start'], 'page_end': packet['page_end'],
              'word_count': packet['word_count'], 'extraction_mode': extraction_mode, 'status': 'not_started'}
    if extraction_mode == 'target_context':
        record.update({key: packet[key] for key in (
            'target_id', 'target_number', 'target_sentence_numbers', 'target_source_sentence_ids',
            'target_word_count', 'target_page_start', 'target_page_end', 'targeting_version')})
    return record


def _whole_packet_prompt(packet: dict[str, Any], source_title: str) -> str:
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
    priority_numbers = extraction_priority_sentence_numbers(packet_sentences)
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
    return (f"Source paper: {source_title}\n"
            f"Section: {packet['section_heading']}\n"
            f"Pages: {packet['page_start']}-{packet['page_end']}\n"
            "PRIORITY EVIDENCE INDEX (ranked; prefer the most specific eligible claims here):\n"
            f"{priority_index or '(no high-value candidate detected)'}\n"
            f"SECTION CONTEXT PACKET:\n{packet_text}")


def _request_packet(packet, packet_record, index, total_packets, client, system, source_title,
                    extraction_mode, claims_per_packet, response_schema, emit):
    check_cancelled()
    packet_started = time.monotonic()
    packet_record['status'] = 'started'
    packet_sentences = packet['sentences']
    label = (f"extraction target {index}/{total_packets} ({packet['target_id']}; packet {packet['packet_number']})"
             if extraction_mode == 'target_context' else f"extraction packet {index}/{total_packets}")
    emit("info", f"Extracting {packet['section_heading']} (pages {packet['page_start']}-{packet['page_end']}), "
         f"{label}; {packet.get('target_word_count', packet['word_count'])} extraction words; "
         f"~{packet['estimated_token_count']} source input tokens")
    def check_extraction_complete(response):
        from backend.providers.structured_output import IncompleteResponseError
        for row in response['claims']:
            required = ('claim_text', 'subject', 'relation', 'object', 'subject_source_mention', 'object_source_mention', 'relation_trigger')
            if any(not str(row.get(field) or '').strip() for field in required):
                raise IncompleteResponseError('An extracted claim omitted required endpoint or source wording')
        check_target_evidence(response, range(1, len(packet_sentences) + 1))
    try:
        user = (targeted_prompt(packet, source_title) if extraction_mode == 'target_context'
                else _whole_packet_prompt(packet, source_title))
        answer_budget = (EXTRACTION_OUTPUT_TOKENS if claims_per_packet else
                         min(12000, max(4000, packet.get('target_word_count', packet['word_count']) * 8)))
        packet_schema = json.loads(json.dumps(response_schema))
        packet_schema['properties']['claims']['items']['properties']['evidence_sentence_numbers']['items']['enum'] = (
            list(range(1, len(packet_sentences) + 1)))
        result = client.chat_json(system, user, max_tokens=answer_budget,
                                  json_schema=packet_schema, request_label=label,
                                  response_check=check_extraction_complete)
    except LLMServiceError as exc:
        packet_record.update(status='service_error', error=str(exc), elapsed_seconds=round(time.monotonic() - packet_started, 3))
        emit('error', f"{label} failed; extraction is incomplete; "
             f"{total_packets - index} extraction regions were not processed")
        raise
    except Exception as exc:
        packet_record.update(status='timeout' if 'timed out' in str(exc).casefold() else 'failed',
                             error=str(exc)[:650], elapsed_seconds=round(time.monotonic() - packet_started, 3))
        emit("warning", f"Extraction response failed for {packet['section_heading']}: {exc}")
        return None
    extracted_items = result.get("claims")
    if not isinstance(extracted_items, list):
        packet_record.update(status='failed', error='Model returned a non-list claims field',
                             elapsed_seconds=round(time.monotonic() - packet_started, 3))
        emit("warning", f"{packet['section_heading']}: model returned a non-list claims field")
        return None
    packet_record.update(status='completed', proposed_claims=len(extracted_items),
                         claim_limit_reached=bool(claims_per_packet and len(extracted_items) >= claims_per_packet),
                         elapsed_seconds=round(time.monotonic() - packet_started, 3))
    return extracted_items


def _proposed_candidate(item: dict[str, Any]) -> dict[str, Any]:
    candidate = {
        'subject': str(item.get('subject', '')).strip(),
        'relation': str(item.get('relation', '')).strip().lower(),
        'object': str(item.get('object', '')).strip(),
        'source_context': str(item.get('source_context') or '').strip()[:300],
        'subject_type': str(item.get('subject_type', '')).strip(),
        'object_type': str(item.get('object_type', '')).strip(),
        'certainty': str(item.get('certainty', '')).strip().lower(),
        'relation_trigger': str(item.get('relation_trigger', '')).strip(),
    }
    raw_qualifiers = item.get('qualifiers')
    candidate['qualifiers'] = {
        key: str(raw_qualifiers.get(key, '')).strip()[:160]
        for key in ('crop_or_species', 'population', 'condition', 'comparator', 'metric')
    } if isinstance(raw_qualifiers, dict) else {}
    return candidate


def _proposed_anchors(item: dict[str, Any]) -> dict[str, str]:
    return {'subject': str(item.get('subject_source_mention', '')).strip(),
            'object': str(item.get('object_source_mention', '')).strip(),
            'relation': str(item.get('relation_trigger', '')).strip()}


def _record_claim_rejection(reason, packet, candidate, item, rejection_records, evidence_record=None):
    record = {'reason': reason, 'packet_number': packet['packet_number'],
              'section_heading': packet['section_heading'], 'proposed_claim': dict(candidate),
              'proposed_source_anchors': _proposed_anchors(item),
              'proposed_claim_text': item.get('claim_text')}
    if evidence_record is not None:
        record.update(source_evidence_sentence_numbers=evidence_record['sentence_numbers'],
                      source_evidence_span=evidence_record['evidence_span'])
    rejection_records.append(record)


def _bind_claim_evidence(item, candidate, packet, extraction_mode, state):
    """Resolve literal sentence provenance without deciding scientific correctness."""
    packet_sentences = packet['sentences']
    evidence = evidence_span_from_item(item, packet_sentences)
    if not candidate['subject'] or not candidate['relation'] or not candidate['object'] or evidence is None:
        state.rejected['malformed'] += 1
        _record_claim_rejection('malformed', packet, candidate, item, state.rejection_records)
        return None
    evidence_numbers, evidence_sentences, evidence_span = evidence
    # Alternate clients may not invoke the provider response_check callback.
    if extraction_mode == 'target_context' and not set(evidence_numbers) <= set(packet['target_sentence_numbers']):
        state.rejected['outside_extraction_target'] += 1
        state.rejection_records.append({'reason': 'outside_extraction_target', 'target_id': packet['target_id'],
                                        'packet_number': packet['packet_number'], 'proposed_claim': candidate,
                                        'source_evidence_sentence_numbers': evidence_numbers})
        return None
    evidence_record = {
        'source_evidence_is_contiguous': evidence_numbers == list(range(evidence_numbers[0], evidence_numbers[-1] + 1)),
        'source_evidence_fragments': [{'sentence_number': n, 'page': sentence['page'], 'text': sentence['text']}
                                      for n, sentence in zip(evidence_numbers, evidence_sentences)],
        'source_intervening_context': [{'sentence_number': n, 'page': packet_sentences[n - 1]['page'],
                                        'text': packet_sentences[n - 1]['text']}
                                       for n in range(evidence_numbers[0], evidence_numbers[-1] + 1) if n not in evidence_numbers],
        'page': evidence_sentences[0]['page'], 'page_start': packet['page_start'], 'page_end': packet['page_end'],
        'section_heading': str(evidence_sentences[0].get('section_heading') or packet['section_heading']),
        'sentence_numbers': evidence_numbers, 'evidence_span': evidence_span,
    }
    review_hints = extraction_review_hints(candidate, evidence_span)
    proposed = _proposed_anchors(item)
    subject_mention = source_surface_mention(evidence_span, proposed['subject'])
    object_mention = source_surface_mention(evidence_span, proposed['object'])
    trigger = source_surface_mention(evidence_span, proposed['relation'])
    if not subject_mention or not object_mention or not trigger:
        state.rejected['unanchored_evidence'] += 1
        _record_claim_rejection('unanchored_evidence', packet, candidate, item, state.rejection_records, evidence_record)
        return None
    subject_mention, object_mention, adjustments = compact_literal_entity_anchors(
        candidate, subject_mention, object_mention, trigger, evidence_span)
    anchor_error = extraction_anchor_error(subject_mention, object_mention, trigger)
    if anchor_error == 'missing_source_anchor':
        state.rejected[anchor_error] += 1
        _record_claim_rejection(anchor_error, packet, candidate, item, state.rejection_records, evidence_record)
        return None
    if anchor_error:
        review_hints.append('review_' + anchor_error)
    evidence_record.update(subject_source_mention=subject_mention, object_source_mention=object_mention,
                           relation_trigger=trigger, anchor_adjustments=adjustments,
                           certainty=candidate['certainty'], subject_type=candidate['subject_type'],
                           object_type=candidate['object_type'], qualifiers=dict(candidate['qualifiers']),
                           source_context=candidate.get('source_context', ''))
    return evidence_record, review_hints


def _link_evidence_citations(evidence_record, packet, bibliography, emit):
    span, page = evidence_record['evidence_span'], evidence_record['page']
    references, links = link_source_citations(bibliography, span, page, packet.get('chapter_id', ''))
    for link in links:
        if link['status'] == 'missing_bibliography_link':
            emit('warning', f"Citation {link['citation_key']} on PDF page {page} has no unambiguous chapter bibliography link")
    evidence_record.update(citation_numbers=citation_numbers(span), citation_keys=author_year_citation_keys(span),
                           references=references, citation_links=links)


def _occurrence_key(candidate, evidence_record):
    """Only the same assertion, restrictions, and source occurrence are merged."""
    return (normalized(candidate['subject']), candidate['relation'], normalized(candidate['object']),
            candidate['certainty'], candidate['subject_type'], candidate['object_type'], evidence_record['page'],
            normalized(evidence_record['evidence_span']), json.dumps(candidate['qualifiers'], sort_keys=True),
            normalized(candidate.get('source_context', '')))


def _merge_overlap_occurrence(existing, evidence_record):
    existing.setdefault('source_evidence_records', []).append(evidence_record)
    existing['source_citation_numbers'] = sorted(set(existing.get('source_citation_numbers', [])) | set(evidence_record['citation_numbers']))
    existing['source_citation_keys'] = sorted(set(existing.get('source_citation_keys', [])) | set(evidence_record['citation_keys']))
    known = {str(item.get('citation_key') or item.get('reference_number')) for item in existing.get('source_references', [])}
    existing.setdefault('source_citation_links', []).extend(
        link for link in evidence_record['citation_links'] if link not in existing.get('source_citation_links', []))
    existing.setdefault('source_references', []).extend(
        item for item in evidence_record['references'] if str(item.get('citation_key') or item.get('reference_number')) not in known)


def refresh_candidate_citations(candidates: list[dict[str, Any]], bibliography: dict[int | str, dict[str, Any]], emit=lambda *args: None) -> list[dict[str, Any]]:
    """Re-link source citations and references against the current bibliography parser."""
    for candidate in candidates:
        evidence_records = candidate.get('source_evidence_records') or []
        if not evidence_records and candidate.get('source_evidence_span'):
            evidence_records = [{
                'evidence_span': candidate['source_evidence_span'],
                'page': candidate.get('source_page', 1),
                'section_heading': candidate.get('source_section_heading', ''),
                'sentence_numbers': candidate.get('source_evidence_sentence_numbers', []),
            }]
        for rec in evidence_records:
            span = rec.get('evidence_span') or candidate.get('source_evidence_span', '')
            page = rec.get('page') or candidate.get('source_page', 1)
            ch = candidate.get('source_chapter_id', '')
            refs, links = link_source_citations(bibliography, span, page, ch)
            rec.update(citation_numbers=citation_numbers(span), citation_keys=author_year_citation_keys(span),
                       references=refs, citation_links=links)
        candidate['source_citation_numbers'] = sorted({n for rec in evidence_records for n in rec.get('citation_numbers', [])})
        candidate['source_citation_keys'] = sorted({k for rec in evidence_records for k in rec.get('citation_keys', [])})
        candidate['source_citation_links'] = [link for rec in evidence_records for link in rec.get('citation_links', [])]
        known = set()
        unique_refs = []
        for rec in evidence_records:
            for ref in rec.get('references', []):
                key = str(ref.get('citation_key') or ref.get('reference_number'))
                if key not in known:
                    known.add(key)
                    unique_refs.append(ref)
        candidate['source_references'] = unique_refs
        if evidence_records:
            candidate['source_evidence_records'] = evidence_records
    return candidates


def _materialize_candidate(item, candidate, evidence_record, packet, context, review_hints):
    document_id, digest, source_title = context.document_id, context.digest, context.source_title
    source_identifiers, document_scope, extraction_mode = context.source_identifiers, context.document_scope, context.extraction_mode
    subject, relation, obj = candidate['subject'], candidate['relation'], candidate['object']
    packet_sentences = packet['sentences']
    evidence_page, evidence_heading = evidence_record['page'], evidence_record['section_heading']
    evidence_numbers, evidence_span = evidence_record['sentence_numbers'], evidence_record['evidence_span']
    evidence_details = {key: evidence_record[key] for key in (
        'source_evidence_is_contiguous', 'source_evidence_fragments', 'source_intervening_context')}
    subject_source_mention, object_source_mention = evidence_record['subject_source_mention'], evidence_record['object_source_mention']
    relation_trigger, anchor_adjustments = evidence_record['relation_trigger'], evidence_record['anchor_adjustments']
    cited_numbers, cited_keys = evidence_record['citation_numbers'], evidence_record['citation_keys']
    cited_references, citation_links = evidence_record['references'], evidence_record['citation_links']
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
                      "qualifier_adjustments": [],
                      "extraction_review_hints": review_hints,
                      "source_context": candidate.get("source_context", ""),
                      "contextual_entity_normalization": None,
                      "extraction_normalizations": [],
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
    return candidate


def _process_packet_claims(extracted_items, packet, context, state, emit):
    for item in extracted_items:
        if not isinstance(item, dict):
            state.rejected['malformed'] += 1
            state.rejection_records.append({'reason': 'malformed', 'packet_number': packet['packet_number'],
                                            'section_heading': packet['section_heading'], 'proposed_claim': {}})
            continue
        candidate = _proposed_candidate(item)
        if normalized(candidate['subject']) == normalized(candidate['object']):
            state.rejected['identical_endpoints'] += 1
            _record_claim_rejection('identical_endpoints', packet, candidate, item, state.rejection_records)
            continue
        if candidate['subject_type'] == 'Unresolved' and candidate['object_type'] == 'Unresolved':
            state.rejected['out_of_domain_unresolved'] += 1
            _record_claim_rejection('out_of_domain_unresolved', packet, candidate, item, state.rejection_records)
            continue
        if candidate['subject_type'] not in CORE_DOMAIN_TYPES and candidate['object_type'] not in CORE_DOMAIN_TYPES:
            state.rejected['out_of_domain_entity'] += 1
            _record_claim_rejection('out_of_domain_entity', packet, candidate, item, state.rejection_records)
            continue
        bound = _bind_claim_evidence(item, candidate, packet, context.extraction_mode, state)
        if bound is None:
            continue
        evidence_record, review_hints = bound
        _link_evidence_citations(evidence_record, packet, context.bibliography, emit)
        key = _occurrence_key(candidate, evidence_record)
        if key in state.seen_occurrences:
            _merge_overlap_occurrence(state.seen_occurrences[key], evidence_record)
            state.rejected['overlap_occurrence_deduplicated'] += 1
            _record_claim_rejection('overlap_occurrence_deduplicated', packet, candidate, item,
                                    state.rejection_records, evidence_record)
            continue
        candidate = _materialize_candidate(item, candidate, evidence_record, packet, context, review_hints)
        state.candidates.append(candidate)
        state.seen_occurrences[key] = candidate


def _load_cached_packet_claims(cache_dir: Path | None, extraction_mode: str) -> dict[str, list[dict[str, Any]]]:
    if not cache_dir:
        return {}
    responses_dir = cache_dir / 'model_responses'
    if not responses_dir.is_dir():
        return {}
    target_claims = {}
    for filename in responses_dir.iterdir():
        if filename.suffix != '.json':
            continue
        try:
            data = json.loads(filename.read_text(encoding='utf-8'))
            if data.get('status') != 'ok':
                continue
            task = data.get('task', '')
            accepted = data.get('accepted_response')
            if not isinstance(accepted, dict) or 'claims' not in accepted:
                continue
            claims = accepted.get('claims') or []
            m = re.search(r'\((target-[0-9a-f]+); packet (\d+)\)', task)
            if m:
                target_claims[m.group(1)] = claims
            else:
                m_pkt = re.search(r'extraction packet (\d+)/', task)
                if m_pkt:
                    target_claims[f"packet_{m_pkt.group(1)}"] = claims
        except Exception:
            continue
    return target_claims


def _iter_packet_extractions(
    packets: list[dict[str, Any]],
    planned_records: list[dict[str, Any]],
    client: OpenCodeClient,
    system: str,
    source_title: str,
    extraction_mode: str,
    claims_per_packet: int,
    response_schema: dict[str, Any],
    emit: Callable[[str, str], None],
    cached_claims: dict[str, list[dict[str, Any]]],
    concurrency: int = 4
):
    total = len(packets)
    to_request = []
    immediate_results = {}

    for index, (pkt, rec) in enumerate(zip(packets, planned_records), 1):
        key = pkt.get('target_id') if extraction_mode == 'target_context' else f"packet_{pkt.get('packet_number')}"
        if key in cached_claims:
            items = cached_claims[key]
            rec.update(status='completed', proposed_claims=len(items),
                       claim_limit_reached=bool(claims_per_packet and len(items) >= claims_per_packet),
                       elapsed_seconds=0.0)
            immediate_results[index - 1] = (pkt, rec, items)
        else:
            to_request.append((index, pkt, rec))

    if immediate_results:
        emit('info', f"Re-hydrated {len(immediate_results)} completed extraction targets from cache; "
                     f"{len(to_request)} remaining targets to evaluate.")

    if not to_request:
        for i in range(total):
            yield immediate_results[i]
        return

    if concurrency <= 1 or len(to_request) <= 1:
        requested_results = {}
        for index, pkt, rec in to_request:
            items = _request_packet(pkt, rec, index, total, client, system,
                                    source_title, extraction_mode, claims_per_packet,
                                    response_schema, emit)
            requested_results[index - 1] = (pkt, rec, items)
        for i in range(total):
            if i in immediate_results:
                yield immediate_results[i]
            elif i in requested_results:
                yield requested_results[i]
        return

    requested_results = {}
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency, thread_name_prefix="extractor") as executor:
        futures = {}
        for index, pkt, rec in to_request:
            check_cancelled()
            time.sleep(0.15)
            fut = executor.submit(
                _request_packet, pkt, rec, index, total, client,
                system, source_title, extraction_mode, claims_per_packet,
                response_schema, emit
            )
            futures[fut] = index - 1

        try:
            for fut in concurrent.futures.as_completed(futures):
                check_cancelled()
                idx = futures[fut]
                items = fut.result()
                requested_results[idx] = (packets[idx], planned_records[idx], items)
        except Exception:
            executor.shutdown(wait=False, cancel_futures=True)
            raise

    for i in range(total):
        if i in immediate_results:
            yield immediate_results[i]
        elif i in requested_results:
            yield requested_results[i]


def extract_claims(pdf_path: Path, client: OpenCodeClient, max_pages: int,
                   emit: Callable[[str, str], None], packets: list[dict[str, Any]] | None = None,
                   document_scope: dict[str, Any] | None = None,
                   source_identifiers: dict[str, str] | None = None,
                   claims_per_packet: int = EXTRACTION_MAX_CLAIMS_PER_PACKET,
                   packet_results: list[dict[str, Any]] | None = None,
                   repair_target: dict[str, Any] | None = None, bibliography=None,
                   extraction_mode: str = 'whole_packet', target_words: int = 800,
                   resume_cache_dir: Path | None = None
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
        if repair_target.get('mode') == 'atomic_reextraction':
            system += ('\nRE-EXTRACT ONLY THE SPECIFIED HELD CLAIM INTO ATOMIC CHILDREN. Return all eligible '
                       'independently testable propositions from its original selected sentences, preserving '
                       'all real restrictions and citations. Use wider context only for explicit referents. '
                       'Every evidence_sentence_numbers value must belong to parent_evidence_sentence_numbers. '
                       'Do not add unrelated packet claims, speculative bridges, editorial emphasis or '
                       'excluded yield/economic assertions. Zero children is valid. Preserve the original '
                       'claim question where faithful; source failure feedback is not proof. '
                       'Each child claim_text must state ONLY that specific child proposition, never the full '
                       'compound sentence. Capability or disposition phrasing (e.g., ability to, can produce) '
                       'must be certainty=descriptive, not causal. TARGET: '
                       + json.dumps(repair_target,ensure_ascii=False))
        else:
            system += ("\nREPAIR ONE EXISTING CLAIM. Return at most one correction of the specified claim, "
                   "using only this PDF context and exact sentence IDs. Use the rejection reason to inspect "
                   "direction, referents, modality and qualifiers again. Do not choose an unrelated claim, "
                   "invent a missing relation, or weaken restrictions just to pass. Preserve the original "
                   "exposure/outcome question where supported; return an empty claims array if no faithful "
                   "correction is possible. The previous verdict is feedback, not proof. REPAIR TARGET: "
                   + json.dumps(repair_target, ensure_ascii=False))
    state = _ExtractionResults()
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
    planned_records = [_packet_coverage_record(packet, extraction_mode) for packet in packets]
    packet_results.extend(planned_records)
    context = _DocumentEvidence(document_id, digest, source_title, source_identifiers, document_scope, bibliography, extraction_mode)
    cached_claims = _load_cached_packet_claims(resume_cache_dir, extraction_mode) if resume_cache_dir else {}
    concurrency = getattr(getattr(client, 'settings', None), 'extraction_concurrency', 1)
    for packet, packet_record, extracted_items in _iter_packet_extractions(
        packets, planned_records, client, system, source_title,
        extraction_mode, claims_per_packet, response_schema, emit,
        cached_claims=cached_claims, concurrency=concurrency
    ):
        if extracted_items is None:
            continue
        _process_packet_claims(extracted_items, packet, context, state, emit)
        emit("success", f"{packet['section_heading']}: {len(state.candidates)} accepted candidates so far")
    emit("info", f"Extraction complete: {len(state.candidates)} unique candidates; "
         f"{state.rejected.get('overlap_occurrence_deduplicated', 0)} overlap duplicates removed; "
         f"{sum(value for key, value in state.rejected.items() if key != 'overlap_occurrence_deduplicated')} discarded ({dict(state.rejected)})")
    return state.candidates, dict(state.rejected), state.rejection_records
