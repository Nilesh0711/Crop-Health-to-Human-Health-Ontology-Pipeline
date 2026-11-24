"""Extract source-anchored claims and retain provenance, citation links and audit records."""

from __future__ import annotations

import json
import re
import time
import concurrent.futures
from copy import deepcopy
from functools import partial
from backend.shared.cancellation import check_cancelled, submit_work
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable
from backend.config.settings import SCHEMA_VERSION
from backend.providers.llm import LLMServiceError, OpenCodeClient
from backend.providers.response_repair import validate_schema
from backend.shared.storage import file_hash, stable_hash, read_jsonl
from backend.domain.claim_context import local_evidence_context
from backend.domain.abbreviations import packet_definitions, relevant_definitions, expand_claim_names
from backend.stages.claim_extraction.anchoring import endpoint_anchor, anchor_diagnostic, ANCHORING_POLICY
from backend.domain.extraction_review import NON_SIGNIFICANCE, SIGNIFICANCE, closed_relation_source_cue, extraction_warnings
from backend.stages.claim_extraction.prompt import build_extraction_prompt
from backend.stages.claim_extraction.targeting import (
    build_extraction_targets, targeted_prompt, check_target_evidence, TARGETING_VERSION, READING_CONTEXT_POLICY,
)
from backend.domain.ontology import schema_error
from backend.shared.text import anchored_quote, normalized, source_surface_mention
from backend.stages.pdf_parsing.layout import PARSING_POLICY
from backend.stages.claim_extraction.contract import (
    ASSOCIATIVE_TRIGGER_RE,
    CLOSED_EXTRACTION_CONTRACT_VERSION,
    CLOSED_EXTRACTION_RESPONSE_SCHEMA,
    CLOSED_PREDICATES,
    CLOSED_PREDICATE_SET_VERSION,
    COMPARATIVE_CUE_RE,
    CORE_PATHWAY_HINT_RE,
    EXTRACTION_MAX_CLAIMS_PER_PACKET,
    EXTRACTION_CONTRACT_VERSION,
    EXTRACTION_OUTPUT_TOKENS,
    EXTRACTION_PATHWAY_TERM_RE,
    EXTRACTION_RELATION_CUE_RE,
    EXTRACTION_RESPONSE_SCHEMA,
    FIGURE_OR_TABLE_RE,
    GENERIC_ENTITIES,
    NON_EMPIRICAL_RE,
    PURPOSE_ONLY_RE,
    closed_predicate_whitelist_digest,
    valid_relation_wording,
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


_DESCRIPTIVE_SUMMARY_RE = re.compile(r'\b(?:rang\w*|varied\s+from|averag\w*|mean\b)\b', re.I)
_UNSUPPORTED_CLOSED_RELATION_WARNINGS = frozenset({
    'possible_unsupported_closed_relation', 'possible_passive_role_reversal_failure', 'possible_agent_role_reversal',
})
_CLOSED_PREDICATE_LOOKUP = {}
for _alias, _entry in CLOSED_PREDICATES.items():
    for _variant in (
        _alias,
        _entry['display_label'],
        _entry['id'],
        _entry['id'].replace(':', '_'),
        _entry['iri'],
    ):
        _CLOSED_PREDICATE_LOOKUP[' '.join(_variant.split()).casefold()] = _alias


def _normalize_closed_predicate_alias(value: Any) -> Any:
    """Normalize exact approved predicate labels, CURIEs, or IRIs to P1-P12."""
    if not isinstance(value, str):
        return value
    return _CLOSED_PREDICATE_LOOKUP.get(' '.join(value.split()).casefold(), value)
_CLOSED_CLAUSE_START_RE = re.compile(
    r'^(?:to\s+[a-z]+|exposed\s+to|when\s+exposed|increasing\s+the\b|decreasing\s+the\b|efficiently\s+[a-z]+)\b', re.I,
)
_CLOSED_INLINE_UNIT_RE = re.compile(
    r'(?:\b\d+(?:\.\d+)?\s*(?:mg|ug|µg|kg|g|ml|l|mmol|µmol|ppm)\b|[≥≤%])', re.I,
)
_CLOSED_BUNDLED_COMP_RE = re.compile(
    r'^(?:higher|lower|greater|increased|decreased|improved|enhanced|reduced)\b.*\band\b', re.I,
)


def _closed_endpoint_hygiene_issue(candidate: dict[str, Any]) -> str | None:
    """Reject noisy verb-clause, inline-unit, or comparative-bundle endpoints cleanly at extraction."""
    for role in ('subject', 'object'):
        value = str(candidate.get(role) or '').strip()
        words = re.findall(r'[\w-]+', value)
        if len(words) > 9:
            return f'{role} endpoint exceeds 9 words ({len(words)} words): keep endpoints as concise noun phrases.'
        if _CLOSED_CLAUSE_START_RE.match(value):
            return f'{role} endpoint starts with a verb or clause phrase instead of a concise noun phrase.'
        if _CLOSED_INLINE_UNIT_RE.search(value):
            return f'{role} endpoint embeds numeric units or inequality symbols that belong in qualifiers.'
        if _CLOSED_BUNDLED_COMP_RE.match(value):
            return f'{role} endpoint bundles comparative terms with "and" instead of clean atomic concepts.'
    return None


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
    if qualifiers.get("comparator"):
        target_context = f"{candidate.get('subject_context') or ''} {candidate.get('object_context') or ''} {qualifiers.get('population') or ''}".strip()
        if not target_context:
            hints.append("review_missing_target_population_for_comparator")
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
    """Resolve selected original sentences, marking gaps rather than inventing continuity."""
    numbers = item.get("evidence_sentence_ids", item.get("evidence_sentence_numbers"))
    if isinstance(numbers, list) and numbers and not any(isinstance(number, bool) or not isinstance(number, int) for number in numbers):
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
    contract_version: int = EXTRACTION_CONTRACT_VERSION
    closed_relations: bool = False


@dataclass
class _ExtractionResults:
    candidates: list[dict[str, Any]] = field(default_factory=list)
    seen_occurrences: dict[tuple[Any, ...], dict[str, Any]] = field(default_factory=dict)
    rejected: Counter[str] = field(default_factory=Counter)
    rejection_records: list[dict[str, Any]] = field(default_factory=list)


def _packet_coverage_record(packet: dict[str, Any], extraction_mode: str,
                            contract_version: int = EXTRACTION_CONTRACT_VERSION) -> dict[str, Any]:
    record = {'packet_number': packet['packet_number'], 'section_heading': packet['section_heading'],
              'page_start': packet['page_start'], 'page_end': packet['page_end'],
              'word_count': packet['word_count'], 'extraction_mode': extraction_mode, 'status': 'not_started'}
    record['output_contract_version'] = contract_version
    record.update(targeting_version=TARGETING_VERSION, reading_context_policy=READING_CONTEXT_POLICY,
                  parsing_policy=PARSING_POLICY, anchoring_policy=ANCHORING_POLICY)
    if extraction_mode == 'target_context':
        record.update({key: packet[key] for key in (
            'target_id', 'target_number', 'target_sentence_numbers', 'target_source_sentence_ids',
            'target_word_count', 'target_page_start', 'target_page_end', 'targeting_version')})
        record.update({key: packet[key] for key in ('reading_context_policy',
            'reading_context_sentence_numbers', 'reading_context_word_count')})
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


def check_extraction_response(response, response_schema, allowed_numbers, target_numbers=None, *, enforce_target=True):
    """Malformed endpoints are schema failures, not token-limit failures."""
    from backend.providers.structured_output import SchemaValidationError
    validate_schema(response, response_schema)
    closed_mode = 'relation_anchor' in response_schema.get('properties', {}).get('claims', {}).get('items', {}).get('required', ())
    for index, row in enumerate(response['claims']):
        for role in (('subject', 'object') if closed_mode else ('subject', 'relation', 'object')):
            if not str(row.get(role) or '').strip():
                raise SchemaValidationError(f'claims.{index}.{role}: provide nonempty source-grounded '
                    'wording; never invent an endpoint to force a single-entity observation into a triplet')
        if not closed_mode and not valid_relation_wording(row['relation']):
            raise SchemaValidationError(f'claims.{index}.relation: use a complete written predicate, not a single-letter code')
        if closed_mode and not str(row.get('relation_anchor') or '').strip():
            raise SchemaValidationError(f'claims.{index}.relation_anchor: provide a nonempty verbatim anchor from the target text')
    check_target_evidence(response, allowed_numbers, target_numbers if enforce_target else None)


def _closed_packet_schema(schema):
    """Quarantine nonconforming relation fields per item without losing valid siblings."""
    schema = deepcopy(schema)
    item = schema['properties']['claims']['items']
    item['required'].remove('predicate_alias')
    for field in ('predicate_alias', 'relation', 'measured_comparison'):
        item['properties'][field] = {}
    return schema


def _request_packet(packet, packet_record, index, total_packets, client, system, source_title,
                    extraction_mode, claims_per_packet, response_schema, emit,
                    *, contract_version: int = EXTRACTION_CONTRACT_VERSION, closed_relations: bool = False):
    check_cancelled()
    packet_started = time.monotonic()
    packet_record['status'] = 'started'
    packet_sentences = packet['sentences']
    if hasattr(client, 'set_request_context'):
        ctx = {'extraction_contract_version': contract_version,
               'targeting_version': TARGETING_VERSION, 'reading_context_policy': READING_CONTEXT_POLICY,
               'parsing_policy': PARSING_POLICY, 'anchoring_policy': ANCHORING_POLICY, 'extraction_mode': extraction_mode}
        if closed_relations:
            ctx.update(predicate_set_version=CLOSED_PREDICATE_SET_VERSION,
                       closed_predicate_whitelist_sha256=closed_predicate_whitelist_digest())
        client.set_request_context(ctx)
    label = (f"extraction target {index}/{total_packets} ({packet['target_id']}; packet {packet['packet_number']})"
             if extraction_mode == 'target_context' else f"extraction packet {index}/{total_packets}")
    target_numbers = packet['target_sentence_numbers'] if extraction_mode == 'target_context' else None
    allowed_numbers = sorted(set(target_numbers or range(1, len(packet_sentences) + 1)) | set(packet.get('reading_context_sentence_numbers', [])))
    packet_schema = _closed_packet_schema(response_schema) if closed_relations else deepcopy(response_schema)
    packet_schema['properties']['claims']['items']['properties']['evidence_sentence_ids']['items']['enum'] = (
        list(allowed_numbers))
    emit("info", f"Extracting {packet['section_heading']} (pages {packet['page_start']}-{packet['page_end']}), "
         f"{label}; {packet.get('target_word_count', packet['word_count'])} extraction words"
         + (f"; {packet['reading_context_word_count']} nearby context words"
            if extraction_mode == 'target_context' else f"; ~{packet['estimated_token_count']} source input tokens"))
    def check_extraction_complete(response):
        # Ownership failures are isolated by the binder per claim. They must
        # not regenerate otherwise valid target findings in the same response.
        check_extraction_response(response, packet_schema, allowed_numbers, target_numbers, enforce_target=False)
    try:
        user = (targeted_prompt(packet, source_title) if extraction_mode == 'target_context'
                else _whole_packet_prompt(packet, source_title))
        answer_budget = (EXTRACTION_OUTPUT_TOKENS if claims_per_packet else
                         min(12000, max(4000, packet.get('target_word_count', packet['word_count']) * 8)))
        result = client.chat_json(system, user, max_tokens=answer_budget,
                                  json_schema=packet_schema, request_label=label,
                                  response_check=check_extraction_complete)
    except LLMServiceError as exc:
        packet_record.update(status='service_error', error=str(exc), elapsed_seconds=round(time.monotonic() - packet_started, 3))
        emit('error', f"{label} could not complete; extraction is incomplete. "
             "Parallel target status cannot be inferred from this target's position; completed response artifacts are retained.")
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
    context_only = sum(not set(item['evidence_sentence_ids']).intersection(target_numbers)
                       for item in extracted_items if isinstance(item, dict)
                       and isinstance(item.get('evidence_sentence_ids'), list)
                       and all(type(n) is int for n in item['evidence_sentence_ids'])) if target_numbers is not None else 0
    packet_record['context_only_claims_quarantined'] = context_only
    if context_only:
        emit('warning' if context_only == len(extracted_items) else 'info',
             f'{label}: {context_only} context-only findings quarantined for audit; '
             'other claims continue through source anchoring; no response regeneration. '
             'Processing coverage does not establish exhaustive claim recall.')
    return extracted_items


_PASSIVE_CUE_RE = re.compile(
    r'\b(?:was|were|is|are|been|be|being)\s+(?:[A-Za-z-]+\s+){0,3}[A-Za-z-]+(?:ed|en)\s+(?:by|with)\b',
    re.I,
)
_POS_CORR_RE = re.compile(r'\bpositiv(?:e|ely)\b', re.I)
_NEG_CORR_RE = re.compile(r'\bnegativ(?:e|ely)\b', re.I)
_CORRELATION_RE = re.compile(r'\bcorrelat(?:e[sd]?|ing|ions?)\b', re.I)
_RELATION_DETAIL_RE = re.compile(
    r'\b(?:significant(?:ly)?|strong(?:ly)?|weak(?:ly)?|modest(?:ly)?|moderate(?:ly)?|slight(?:ly)?|'
    r'linear(?:ly)?|direct(?:ly)?|indirect(?:ly)?|partial(?:ly)?|mainly|largely|predominantly|high(?:est)?)\b',
    re.I,
)


def _verify_passive_roles(evidence_sentences, trigger, relation_text, subject_mention, object_mention, predicate_alias):
    """Verify whether passive source evidence was properly reversed into the forward property's subject->object order."""
    meta = CLOSED_PREDICATES.get(predicate_alias or '')
    if not meta or not meta.get('supports_passive_reversal'):
        return None, False
    passive_match = None
    passive_sentence = ''
    trig_passive = bool(_PASSIVE_CUE_RE.search(trigger or ''))
    rel_passive = bool(_PASSIVE_CUE_RE.search(relation_text or ''))
    for sent in evidence_sentences:
        text = sent['text']
        trig_span = source_surface_mention(text, trigger) if trigger else None
        if not trig_span:
            continue
        trig_m = re.search(rf'(?<!\w){re.escape(trig_span)}(?!\w)', text, re.I)
        for m in _PASSIVE_CUE_RE.finditer(text):
            overlaps_trigger = bool(trig_m and not (m.end() <= trig_m.start() or m.start() >= trig_m.end()))
            if trig_passive or rel_passive or overlaps_trigger:
                passive_match = m
                passive_sentence = text
                break
        if passive_match is not None:
            break
    if passive_match is None and (trig_passive or rel_passive):
        for sent in evidence_sentences:
            m = _PASSIVE_CUE_RE.search(sent['text'])
            if m:
                passive_match = m
                passive_sentence = sent['text']
                break
    if passive_match is None:
        return None, False
    # Check whether Patient (object) precedes the passive verb and Agent (subject) follows by/with.
    prefix = passive_sentence[:passive_match.start()]
    suffix = passive_sentence[passive_match.end():]
    obj_before = bool(source_surface_mention(prefix, object_mention))
    subj_after = bool(source_surface_mention(suffix, subject_mention))
    subj_before = bool(source_surface_mention(prefix, subject_mention))
    obj_after = bool(source_surface_mention(suffix, object_mention))
    if obj_before and subj_after and not (subj_before and obj_after):
        return True, True
    if subj_before and obj_after:
        return False, True
    return False, True


def _closed_relation_detail_loss(alias: str, trigger: str, metric: str) -> tuple[str, list[str]]:
    text = trigger
    omitted = []
    if alias == 'P5':
        for label, pattern in (('positive correlation sign', _POS_CORR_RE), ('negative correlation sign', _NEG_CORR_RE),
                               ('correlation significance', SIGNIFICANCE)):
            if bool(pattern.search(trigger)) != bool(pattern.search(metric)):
                omitted.append(label)
        text = SIGNIFICANCE.sub('', text)
        if not _CORRELATION_RE.search(trigger) or NON_SIGNIFICANCE.search(f'{trigger} {metric}'):
            omitted.append('affirmed correlation not established')
    if _RELATION_DETAIL_RE.search(text):
        omitted.append('magnitude, significance, or directness qualification')
    if (not omitted and alias == 'P3' and re.search(r'\b(?:affect\w*|influenc\w*)\b', text, re.I)
            and not re.search(r'\bcausal(?:ly)?\b', trigger, re.I)):
        omitted.append('non-specific causal verb qualification')
    omitted = list(dict.fromkeys(omitted))
    return ('broader' if omitted else 'exact'), omitted


def _proposed_candidate(item: dict[str, Any]) -> dict[str, Any]:
    candidate = {
        'subject': str(item.get('subject', '')).strip(),
        'relation': str(item.get('relation', '')).strip(),
        'object': str(item.get('object', '')).strip(),
    }
    alias = item.get('predicate_alias')
    if alias is not None:
        candidate['predicate_alias'] = str(alias).strip()
        if candidate['predicate_alias'] in CLOSED_PREDICATES:
            entry = CLOSED_PREDICATES[candidate['predicate_alias']]
            candidate['relation'] = entry['display_label']
            candidate['predicate_set_version'] = CLOSED_PREDICATE_SET_VERSION
            candidate['proposed_relation_iri'] = entry['iri']
            candidate['proposed_relation_id'] = entry['id']
            candidate['proposed_relation_display_label'] = entry['display_label']
    for role in ('subject', 'object'):
        if item.get(role + '_context'):
            candidate[role + '_context'] = item[role + '_context'].strip()
    if item.get('measured_comparison'):
        candidate['measured_comparison'] = dict(item['measured_comparison'])
    raw_qualifiers = item.get('qualifiers')
    candidate['qualifiers'] = {
        key: str(value).strip() for key, value in raw_qualifiers.items()
        if key in {'crop_or_species', 'population', 'condition', 'comparator', 'metric'} and isinstance(value, str)
    } if isinstance(raw_qualifiers, dict) else {}
    hints = {role: item[role + '_type'] for role in ('subject', 'object') if item.get(role + '_type')}
    if hints:
        candidate['extraction_type_hints'] = hints
    return candidate


def _proposed_anchors(item: dict[str, Any]) -> dict[str, str]:
    if 'evidence_sentence_ids' in item:
        return {role: str(item.get(role + '_anchor') or item.get(role, '')).strip() for role in ('subject', 'object')} | {
            'relation': str(item.get('relation_anchor') or item.get('relation', '')).strip()}
    return {'subject': str(item.get('subject_source_mention', '')).strip(),
            'object': str(item.get('object_source_mention', '')).strip(),
            'relation': str(item.get('relation_trigger', '')).strip()}


def _record_claim_rejection(reason, packet, candidate, item, rejection_records, evidence_record=None):
    record = {'reason': reason, 'packet_number': packet['packet_number'],
              'section_heading': packet['section_heading'], 'proposed_claim': dict(candidate),
              'proposed_source_anchors': _proposed_anchors(item)}
    for key in ('target_id', 'target_number', 'target_sentence_numbers', 'reading_context_sentence_numbers'):
        if key in packet:
            record[key] = packet[key]
    if evidence_record is not None:
        record.update(source_evidence_sentence_numbers=evidence_record['sentence_numbers'],
                      source_evidence_span=evidence_record['evidence_span'])
        if evidence_record.get('anchor_failures'):
            record['anchor_failures'] = evidence_record['anchor_failures']
    rejection_records.append(record)


def _bind_claim_evidence(item, candidate, packet, extraction_mode, state):
    """Resolve literal sentence provenance without deciding scientific correctness."""
    packet_sentences = packet['sentences']
    evidence = evidence_span_from_item(item, packet_sentences)
    if not candidate['subject'] or not candidate['relation'] or not candidate['object'] or evidence is None:
        state.rejected['malformed'] += 1
        _record_claim_rejection('malformed', packet, candidate, item, state.rejection_records)
        return None
    if not valid_relation_wording(candidate['relation']):
        state.rejected['malformed_relation'] += 1
        _record_claim_rejection('malformed_relation', packet, candidate, item, state.rejection_records)
        return None
    evidence_numbers, evidence_sentences, evidence_span = evidence
    # Alternate clients may not invoke the provider response_check callback.
    if extraction_mode == 'target_context' and not set(evidence_numbers) <= set(packet['target_sentence_numbers']) | set(packet.get('reading_context_sentence_numbers', [])):
        state.rejected['outside_extraction_target'] += 1
        state.rejection_records.append({'reason': 'outside_extraction_target', 'target_id': packet['target_id'],
                                        'packet_number': packet['packet_number'], 'proposed_claim': candidate,
                                        'source_evidence_sentence_numbers': evidence_numbers})
        return None
    if extraction_mode == 'target_context' and not set(evidence_numbers).intersection(packet['target_sentence_numbers']):
        state.rejected['context_only_finding'] += 1
        _record_claim_rejection('context_only_finding', packet, candidate, item,
                                state.rejection_records, {'sentence_numbers': evidence_numbers, 'evidence_span': evidence_span})
        return None
    evidence_record = {
        'target_sentence_numbers': sorted(set(evidence_numbers) & set(packet.get('target_sentence_numbers', evidence_numbers))),
        'supporting_context_sentence_numbers': sorted(set(evidence_numbers) - set(packet.get('target_sentence_numbers', evidence_numbers))),
        'source_evidence_is_contiguous': evidence_numbers == list(range(evidence_numbers[0], evidence_numbers[-1] + 1)),
        'source_evidence_fragments': [{'sentence_number': n, 'page': sentence['page'], 'text': sentence['text'],
                                       **({'appended_context': sentence['appended_context']} if sentence.get('appended_context') else {})}
                                      for n, sentence in zip(evidence_numbers, evidence_sentences)],
        'source_intervening_context': [{'sentence_number': n, 'page': packet_sentences[n - 1]['page'],
                                        'text': packet_sentences[n - 1]['text']}
                                       for n in range(evidence_numbers[0], evidence_numbers[-1] + 1)
                                       if n not in evidence_numbers and not packet_sentences[n - 1].get('appended_context')],
        'page': evidence_sentences[0]['page'], 'page_start': packet['page_start'], 'page_end': packet['page_end'],
        'section_heading': str(evidence_sentences[0].get('section_heading') or packet['section_heading']),
        'sentence_numbers': evidence_numbers, 'evidence_span': evidence_span,
    }
    review_hints = extraction_review_hints(candidate, evidence_span)
    proposed = _proposed_anchors(item)
    subject_mention = object_mention = None
    closed_mode = bool(candidate.get('predicate_alias'))
    target_anchor_required = closed_mode or (bool(candidate.get('measured_comparison')) and 'relation_anchor' in item)
    if target_anchor_required:
        raw_rel_anchor = str(item.get('relation_anchor') or '').strip()
        trigger = source_surface_mention(evidence_span, raw_rel_anchor) if raw_rel_anchor else None
    else:
        trigger = source_surface_mention(evidence_span, proposed['relation'])
    minimal = 'evidence_sentence_ids' in item and not target_anchor_required
    target_set = set(packet.get('target_sentence_numbers', evidence_numbers))
    target_texts = [s['text'] for n, s in zip(evidence_numbers, evidence_sentences) if n in target_set]
    trigger_in_target = bool(trigger and (extraction_mode != 'target_context'
                                          or any(source_surface_mention(t, trigger) for t in target_texts)))
    coordination_adjustments = []
    anchor_failures = []
    for role in ('subject', 'object'):
        match = endpoint_anchor(candidate[role], proposed[role],
                                [s['text'] for s in evidence_sentences],
                                packet.get('source_abbreviation_definitions', []),
                                allow_canonical=closed_mode)
        if match:
            anchor, basis, definitions_used = match
            if role == 'subject':
                subject_mention = anchor
            else:
                object_mention = anchor
            coordination_adjustments.append({'role': role, 'mention': anchor,
                'basis': basis, 'atomic_endpoint': candidate[role], 'policy': ANCHORING_POLICY,
                'source_sentence_numbers': [n for n, s in zip(evidence_numbers, evidence_sentences)
                                            if anchor in s['text']],
                'reused_words': candidate[role].split() if basis == 'bounded_coordination' else []})
            if definitions_used:
                coordination_adjustments[-1]['definitions'] = definitions_used
            if basis != 'literal_endpoint':
                review_hints.append('review_' + basis + '_anchor_' + role)
        else:
            anchor_failures.append({'role': role, 'endpoint': candidate[role],
                'proposed_anchor': proposed[role], 'category': anchor_diagnostic(candidate[role], proposed[role],
                    [s['text'] for s in evidence_sentences]), 'policy': ANCHORING_POLICY})
    if not trigger and target_anchor_required:
        anchor_failures.append({'role': 'relation', 'endpoint': candidate['relation'],
            'proposed_anchor': proposed['relation'], 'category': 'unsupported_relation_anchor', 'policy': ANCHORING_POLICY})
    elif target_anchor_required and not trigger_in_target:
        anchor_failures.append({'role': 'relation', 'endpoint': candidate['relation'],
            'proposed_anchor': proposed['relation'], 'category': 'context_only_relation_anchor', 'policy': ANCHORING_POLICY})
    if not subject_mention or not object_mention or (not trigger and not minimal) or (target_anchor_required and not trigger_in_target):
        state.rejected['unanchored_evidence'] += 1
        _record_claim_rejection('unanchored_evidence', packet, candidate, item, state.rejection_records,
                                {**evidence_record, 'anchor_failures': anchor_failures})
        return None
    if closed_mode:
        adjustments = coordination_adjustments
        anchor_error = None
        passive_ok, is_passive = _verify_passive_roles(
            evidence_sentences, trigger or '', candidate['relation'],
            subject_mention, object_mention, candidate.get('predicate_alias'),
        )
        candidate['passive_source_evidence'] = is_passive
        if passive_ok is not None:
            candidate['passive_role_reversal_verified'] = passive_ok
            if not passive_ok:
                review_hints.append('review_passive_role_reversal')
        rel_map, detail_loss = _closed_relation_detail_loss(
            candidate['predicate_alias'],
            ' '.join(closed_relation_source_cue(text, trigger) for text in target_texts),
            candidate['qualifiers'].get('metric', ''),
        )
        candidate['proposed_relation_mapping'] = rel_map
        candidate['proposed_relation_detail_loss'] = detail_loss
    elif target_anchor_required or minimal:
        adjustments = coordination_adjustments
        if not trigger:
            review_hints.append('review_nonliteral_relation_wording')
        anchor_error = None
    else:
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
                           relation_trigger=trigger or '', anchor_adjustments=adjustments,
                           qualifiers=dict(candidate['qualifiers']),
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
    key = (normalized(candidate['subject']), candidate['relation'], normalized(candidate['object']),
           evidence_record['page'], json.dumps(candidate.get('measured_comparison'), sort_keys=True),
           normalized(evidence_record['evidence_span']), json.dumps(candidate['qualifiers'], sort_keys=True),
           normalized(candidate.get('source_context', '')),
           normalized(candidate.get('subject_context', '')), normalized(candidate.get('object_context', '')))
    if candidate.get('predicate_alias'):
        key += (candidate['predicate_alias'], candidate.get('proposed_relation_iri', ''))
    return key


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
    contract_version = context.contract_version
    alias_suffix = f"|alias:{candidate['predicate_alias']}|{candidate.get('proposed_relation_iri', '')}" if candidate.get('predicate_alias') else ''
    candidate_id = "candidate-" + stable_hash(
        f"{document_id}|{normalized(subject)}|{relation}|{normalized(obj)}|"
        f"v{contract_version}|"
        f"{json.dumps(candidate['qualifiers'], sort_keys=True)}|{evidence_page}|{normalized(evidence_span)}"
        + (json.dumps(candidate['measured_comparison'], sort_keys=True) if candidate.get('measured_comparison') else '')
        + f"|subject_context:{normalized(candidate.get('subject_context', ''))}|object_context:{normalized(candidate.get('object_context', ''))}"
        + (f"|context:{normalized(candidate['source_context'])}" if normalized(candidate.get('source_context', '')) else '')
        + alias_suffix, 32)
    candidate.update({**evidence_details, "schema_version": SCHEMA_VERSION, "candidate_id": candidate_id,
                      "extraction_contract_version": contract_version,
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
                      "source_evidence_target_sentence_numbers": evidence_record['target_sentence_numbers'],
                      "source_evidence_context_sentence_numbers": evidence_record['supporting_context_sentence_numbers'],
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
                      "source_reading_context": '\n'.join(
                          f"[{n}] [page {sentence['page']}] {sentence['text']}"
                          for n, sentence in enumerate(packet_sentences, 1)
                          if extraction_mode != 'target_context'
                          or n in set(packet.get('target_sentence_numbers', ())) | set(packet.get('reading_context_sentence_numbers', ()))),
                      })
    if extraction_mode == 'target_context':
        candidate.update(source_extraction_mode=extraction_mode, source_target_id=packet['target_id'],
                         source_target_sentence_numbers=packet['target_sentence_numbers'],
                         source_target_sentence_ids=packet['target_source_sentence_ids'],
                         source_extraction_reading_context_sentence_numbers=packet['reading_context_sentence_numbers'],
                         source_extraction_reading_context_policy=packet['reading_context_policy'])
    candidate["schema_error"] = schema_error(candidate)
    candidate['extraction_warnings'] = extraction_warnings(candidate)
    candidate['extraction_review_status'] = 'held' if candidate['extraction_warnings'] else 'not_flagged'
    return candidate


def _process_packet_claims(extracted_items, packet, context, state, emit,
                           response_schema=EXTRACTION_RESPONSE_SCHEMA):
    for item in extracted_items:
        if not isinstance(item, dict):
            state.rejected['malformed'] += 1
            state.rejection_records.append({'reason': 'malformed', 'packet_number': packet['packet_number'],
                                            'section_heading': packet['section_heading'], 'proposed_claim': {}})
            continue
        if context.closed_relations and (
            'predicate_alias' not in item or 'relation' in item or 'measured_comparison' in item
        ):
            reason = 'invalid_finding_kind'
            evidence = evidence_span_from_item(item, packet['sentences'])
            evidence_record = {'sentence_numbers': evidence[0], 'evidence_span': evidence[2]} if evidence else None
            state.rejected[reason] += 1
            _record_claim_rejection(reason, packet, item, item, state.rejection_records, evidence_record)
            continue
        if context.closed_relations and isinstance(item.get('predicate_alias'), str):
            normalized_alias = _normalize_closed_predicate_alias(item['predicate_alias'])
            if normalized_alias != item['predicate_alias']:
                item = {**item, 'predicate_alias': normalized_alias}
        try:
            validate_schema({'claims': [item]}, response_schema)
        except ValueError:
            reason = ('malformed_predicate_alias' if context.closed_relations
                      and not any(item.get('predicate_alias') == alias for alias in CLOSED_PREDICATES)
                      else 'malformed')
            state.rejected[reason] += 1
            state.rejection_records.append({'reason': reason, 'packet_number': packet['packet_number'],
                                            'section_heading': packet['section_heading'], 'proposed_claim': item})
            continue
        candidate = _proposed_candidate(item)
        if normalized(candidate['subject']) == normalized(candidate['object']):
            state.rejected['identical_endpoints'] += 1
            evidence = evidence_span_from_item(item, packet['sentences'])
            evidence_record = ({'sentence_numbers': evidence[0], 'evidence_span': evidence[2]}
                               if evidence is not None else None)
            _record_claim_rejection('identical_endpoints', packet, candidate, item,
                                    state.rejection_records, evidence_record)
            if (candidate.get('qualifiers') or {}).get('metric') and _DESCRIPTIVE_SUMMARY_RE.search(candidate['relation']):
                state.rejection_records[-1]['descriptive_observation'] = True
            continue
        bound = _bind_claim_evidence(item, candidate, packet, context.extraction_mode, state)
        if bound is None:
            continue
        evidence_record, review_hints = bound
        _link_evidence_citations(evidence_record, packet, context.bibliography, emit)
        key = _occurrence_key(candidate, evidence_record)
        resolved = _materialize_candidate(item, dict(candidate), evidence_record, packet, context, review_hints)
        if context.closed_relations and not resolved.get('measured_comparison'):
            warnings = [w for w in resolved['extraction_warnings'] if w['code'] in _UNSUPPORTED_CLOSED_RELATION_WARNINGS]
            reason = ''
            if warnings:
                reason = 'unsupported_closed_relation'
                candidate['extraction_warnings'] = warnings
            elif resolved['proposed_relation_mapping'] != 'exact':
                reason = 'non_exact_closed_relation'
                candidate['proposed_relation_detail_loss'] = resolved.get('proposed_relation_detail_loss', [])
            elif hygiene := _closed_endpoint_hygiene_issue(resolved):
                reason = 'noisy_endpoint_rejected'
                candidate['extraction_warnings'] = [
                    *resolved['extraction_warnings'],
                    {'code': 'noisy_endpoint', 'message': hygiene, 'source_text': resolved.get('source_evidence_span', '')},
                ]
            elif resolved['extraction_warnings']:
                reason = 'extraction_quality_rejected'
                candidate['extraction_warnings'] = resolved['extraction_warnings']
            if reason:
                state.rejected[reason] += 1
                _record_claim_rejection(reason, packet, candidate, item, state.rejection_records, evidence_record)
                continue
        if key in state.seen_occurrences:
            _merge_overlap_occurrence(state.seen_occurrences[key], evidence_record)
            state.rejected['overlap_occurrence_deduplicated'] += 1
            _record_claim_rejection('overlap_occurrence_deduplicated', packet, candidate, item,
                                    state.rejection_records, evidence_record)
            continue
        state.candidates.append(resolved)
        state.seen_occurrences[key] = resolved


def _load_cached_packet_claims(cache_dir: Path | None, extraction_mode: str,
                               *, contract_version: int = EXTRACTION_CONTRACT_VERSION,
                               response_schema=EXTRACTION_RESPONSE_SCHEMA,
                               closed_relations: bool = False) -> dict[str, list[dict[str, Any]]]:
    if not cache_dir:
        return {}
    responses_dir = cache_dir / 'model_responses'
    if not responses_dir.is_dir():
        return {}
    coverage = read_jsonl(cache_dir / 'extraction_packets.jsonl')
    validation_schema = _closed_packet_schema(response_schema) if closed_relations else response_schema
    request_log = cache_dir / 'llm_requests.jsonl'
    logged_names = (list(dict.fromkeys(
        Path(r['response_artifact']).name for r in read_jsonl(request_log)
        if r.get('stage') == 'extraction' and r.get('status') == 'ok' and r.get('response_artifact')
    )) if closed_relations and request_log.is_file() else None)
    legacy_metadata_verified = False
    checkpoint_path = cache_dir / 'pipeline_checkpoint.json'
    if logged_names and checkpoint_path.is_file():
        checkpoint = json.loads(checkpoint_path.read_bytes())
        legacy_metadata_verified = (checkpoint.get('extraction_contract_version') == contract_version
            and checkpoint.get('predicate_set_version') == CLOSED_PREDICATE_SET_VERSION
            and checkpoint.get('predicate_whitelist_sha256') == closed_predicate_whitelist_digest())
    current = {r.get('target_id') if extraction_mode == 'target_context' else f"packet_{r.get('packet_number')}"
               for r in coverage if r.get('output_contract_version') == contract_version
               and r.get('status') == 'completed' and r.get('extraction_mode') == extraction_mode
               and r.get('targeting_version') == TARGETING_VERSION
               and r.get('reading_context_policy') == READING_CONTEXT_POLICY
               and r.get('parsing_policy') == PARSING_POLICY
               and r.get('anchoring_policy') == ANCHORING_POLICY}
    target_claims = {}
    # Raw response files from earlier runs can remain in the same directory.
    filenames = (responses_dir / name for name in logged_names) if logged_names is not None else responses_dir.iterdir()
    for filename in filenames:
        if filename.suffix != '.json':
            continue
        try:
            data = json.loads(filename.read_text(encoding='utf-8'))
            if data.get('status') != 'ok':
                continue
            if data.get('extraction_contract_version') != contract_version:
                continue
            if closed_relations:
                saved_predicates = (data.get('predicate_set_version'), data.get('closed_predicate_whitelist_sha256'))
                if (saved_predicates != (CLOSED_PREDICATE_SET_VERSION, closed_predicate_whitelist_digest())
                        and not (saved_predicates == (None, None) and legacy_metadata_verified)):
                    continue
            if (data.get('targeting_version') != TARGETING_VERSION
                    or data.get('reading_context_policy') != READING_CONTEXT_POLICY
                    or data.get('parsing_policy') != PARSING_POLICY
                    or data.get('anchoring_policy') != ANCHORING_POLICY
                    or data.get('extraction_mode') != extraction_mode):
                continue
            task = data.get('task', '')
            accepted = data.get('accepted_response')
            if not isinstance(accepted, dict) or 'claims' not in accepted:
                continue
            claims = accepted.get('claims') or []
            # Historical responses remain archived. Reusing them as new outputs
            # would silently bypass the current extraction contract.
            validate_schema(accepted, validation_schema)
            if not closed_relations and any(not valid_relation_wording(row.get('relation')) for row in claims):
                continue
            m = re.search(r'\((target-[0-9a-f]+); packet (\d+)\)', task)
            if m:
                if m.group(1) in current:
                    target_claims[m.group(1)] = claims
            else:
                m_pkt = re.search(r'extraction packet (\d+)/', task)
                if m_pkt and f"packet_{m_pkt.group(1)}" in current:
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
    concurrency: int = 4,
    contract_version: int = EXTRACTION_CONTRACT_VERSION,
    closed_relations: bool = False,
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
                                    response_schema, emit, contract_version=contract_version,
                                    closed_relations=closed_relations)
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
            fut = submit_work(
                executor,
                partial(
                    _request_packet,
                    pkt, rec, index, total, client,
                    system, source_title, extraction_mode, claims_per_packet,
                    response_schema, emit,
                    contract_version=contract_version, closed_relations=closed_relations,
                ),
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
                   resume_cache_dir: Path | None = None, scope_instructions: str | None = None,
                   prepared_targets: bool = False,
                   context_packets: list[dict[str, Any]] | None = None,
                   closed_relations: bool = False,
                   reference=None,
                   ) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    digest = file_hash(pdf_path)
    document_id = "local-" + stable_hash(f"{pdf_path.stem}|{digest}", 32)
    source_title = source_paper_title(pdf_path)
    source_identifiers = source_identifiers or source_article_identifiers(pdf_path)
    if extraction_mode not in {'whole_packet', 'target_context'}:
        raise ValueError('Unknown extraction mode')
    if prepared_targets and (extraction_mode != 'target_context' or not packets or repair_target is not None):
        raise ValueError('Frozen targets require explicit target/context packets and no repair target')
    # A claim repair already has an explicit single-claim target. Retain its
    # original packet IDs rather than scheduling unrelated new regions.
    if repair_target is not None:
        extraction_mode = 'whole_packet'
    contract_version = CLOSED_EXTRACTION_CONTRACT_VERSION if closed_relations else EXTRACTION_CONTRACT_VERSION
    base_schema = CLOSED_EXTRACTION_RESPONSE_SCHEMA if closed_relations else EXTRACTION_RESPONSE_SCHEMA
    system = build_extraction_prompt(claims_per_packet, extraction_mode,
                                     closed_relations=closed_relations, reference=reference)
    if scope_instructions:
        system += '\n\n' + scope_instructions
    if repair_target is not None:
        if repair_target.get('mode') == 'atomic_reextraction':
            system += ('\nRE-EXTRACT ONLY THE SPECIFIED HELD CLAIM INTO ATOMIC CHILDREN. Return all eligible '
                       'independently testable propositions from its original selected sentences, preserving '
                       'all real restrictions and citations. Use wider context only for explicit referents. '
                       'Every evidence_sentence_ids value must belong to parent_evidence_sentence_numbers. '
                       'Do not add unrelated packet claims, speculative bridges, editorial emphasis or '
                       'excluded yield/economic assertions. Zero children is valid. Preserve the original '
                       'claim question where faithful; source failure feedback is not proof. '
                       'Each child triple must state ONLY that specific child proposition. Preserve capability '
                       'and possibility wording in relation/qualifiers without converting it to a definite effect. TARGET: '
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
    response_schema = json.loads(json.dumps(base_schema))
    if claims_per_packet:
        response_schema["properties"]["claims"]["maxItems"] = claims_per_packet
    packets = packets if packets is not None else list(iter_pdf_context_packets(pdf_path, max_pages))
    if not packets:
        raise ValueError("The PDF has no extractable text. OCR is required before extraction.")
    doc_packets = context_packets if context_packets is not None else packets
    abbreviation_glossary = packet_definitions(doc_packets, document_id)
    if prepared_targets:
        # Evaluation adapters may replay frozen production targets. Never
        # resegment them or silently mix old IDs with a different context policy.
        for packet in packets:
            if (packet.get('targeting_version') != TARGETING_VERSION
                    or packet.get('reading_context_policy') != READING_CONTEXT_POLICY
                    or not packet.get('target_id') or not packet.get('target_sentence_numbers')
                    or not (set(packet['target_sentence_numbers']) | set(packet.get('reading_context_sentence_numbers', [])))
                    <= set(range(1, len(packet['sentences']) + 1))
                    or packet.get('frozen_source_pdf_sha256', digest) != digest):
                raise ValueError('Frozen production target is incompatible or has invalid sentence IDs')
    elif extraction_mode == 'target_context':
        packets = build_extraction_targets(packets, target_words,
            abbreviation_glossary=abbreviation_glossary, document_id=document_id)
        emit('info', f"Target/context extraction: {len(packets)} targets; about {target_words} words per target; "
             "nearby paragraphs and source-defined abbreviations supplied; full packets stored; no LLM discovery pass")
    packet_results = packet_results if packet_results is not None else []
    planned_records = [_packet_coverage_record(packet, extraction_mode, contract_version) for packet in packets]
    packet_results.extend(planned_records)
    context = _DocumentEvidence(document_id, digest, source_title, source_identifiers, document_scope, bibliography,
                                extraction_mode, contract_version=contract_version, closed_relations=closed_relations)
    cached_claims = (_load_cached_packet_claims(resume_cache_dir, extraction_mode,
                                                contract_version=contract_version,
                                                response_schema=base_schema,
                                                closed_relations=closed_relations)
                     if resume_cache_dir else {})
    concurrency = getattr(getattr(client, 'settings', None), 'extraction_concurrency', 1)
    for packet, packet_record, extracted_items in _iter_packet_extractions(
        packets, planned_records, client, system, source_title,
        extraction_mode, claims_per_packet, response_schema, emit,
        cached_claims=cached_claims, concurrency=concurrency,
        contract_version=contract_version, closed_relations=closed_relations,
    ):
        if extracted_items is None:
            continue
        _process_packet_claims(extracted_items, packet, context, state, emit, response_schema=base_schema)
        emit("success", f"{packet['section_heading']}: {len(state.candidates)} accepted candidates so far")
    emit("info", f"Extraction complete: {len(state.candidates)} unique candidates; "
         f"{state.rejected.get('overlap_occurrence_deduplicated', 0)} overlap duplicates removed; "
         f"{sum(value for key, value in state.rejected.items() if key != 'overlap_occurrence_deduplicated')} discarded ({dict(state.rejected)})")
    finalize_source_context(state.candidates, doc_packets, context.document_id, context.digest, abbreviation_glossary)
    return state.candidates, dict(state.rejected), state.rejection_records


def finalize_source_context(candidates, packets, document_id, pdf_sha256, abbreviation_glossary):
    """Shared production/replay finalisation, preserving literal source anchors."""
    for candidate in candidates:
        candidate['source_abbreviation_definitions'] = relevant_definitions(candidate, abbreviation_glossary)
        candidate.update(expand_claim_names(candidate, abbreviation_glossary))
    # The source-defined expansion can reveal a pure time/measurement span.
    # Reuse the authoritative warning function after expansion, not a rewrite.
    from backend.domain.treatment_labels import document_treatment_labels, labels_for_claim
    labels = document_treatment_labels(packets, document_id, pdf_sha256)
    for candidate in candidates:
        candidate['source_treatment_labels'] = labels_for_claim(candidate, labels)
        candidate['extraction_warnings'] = extraction_warnings(candidate)
        candidate['extraction_review_status'] = 'held' if candidate['extraction_warnings'] else 'not_flagged'
    return candidates
