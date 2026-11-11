"""Assign bounded extraction regions without an LLM discovery/filtering pass."""

from copy import deepcopy
import json

from backend.shared.storage import stable_hash
from backend.stages.pdf_parsing.packets import word_count
from backend.domain.abbreviations import packet_definitions, relevant_definitions


TARGETING_VERSION = 5
READING_CONTEXT_POLICY = 'bounded-sentence-supporting-evidence-v3'
READING_CONTEXT_WORDS = 250  # Hard context allowance; source sentences are never cut.
NEIGHBOR_PARAGRAPHS = 2


def check_target_evidence(response, allowed_numbers, target_numbers=None):
    from backend.providers.structured_output import SchemaValidationError

    allowed = set(allowed_numbers)
    for index, claim in enumerate(response['claims']):
        numbers = claim.get('evidence_sentence_ids', claim.get('evidence_sentence_numbers')) or []
        if (not numbers or any(type(number) is not int for number in numbers)
                or not set(numbers) <= allowed
                or numbers != sorted(set(numbers))):
            raise SchemaValidationError(f'claims.{index}.evidence_sentence_ids={numbers!r}: '
                f'use unique, ascending supplied sentence IDs; allowed input IDs={sorted(allowed)}. '
                'Non-adjacent IDs are permitted; their gaps remain explicit for source review. Do not renumber sentences.')
        if target_numbers is not None and not set(numbers).intersection(target_numbers):
            raise SchemaValidationError(f'claims.{index}.evidence_sentence_ids={numbers!r}: context-only finding; '
                f'target finding IDs={sorted(target_numbers)}; support-only IDs={sorted(allowed - set(target_numbers))}. '
                'Context can support a target finding, but cannot supply a separate finding. Do not append an unrelated target ID.')


def sentence_identity(sentence, packet):
    """Identify the same parsed occurrence across overlapping context packets."""
    return sentence.get('source_sentence_id') or stable_hash('|'.join(str(value) for value in (
        sentence.get('chapter_id', packet.get('chapter_id', 'chapter-1')),
        sentence.get('page'), sentence.get('section_heading', packet['section_heading']),
        sentence.get('paragraph_id'), sentence.get('page_sentence_index'), sentence['text'],
    )), 32)


def build_extraction_targets(packets, target_words=200, *, abbreviation_glossary=None, document_id='current-document'):
    """Keep original reading packets; assign each distinct occurrence once.

    Targets prefer paragraph boundaries and never cross chapters. Multiple
    nearby headings may share a target; each sentence retains its own heading.
    Oversized paragraphs split at sentence boundaries. A single long sentence
    remains intact. Every parsed sentence is assigned, including those without
    domain keywords. Processing coverage is not a claim of exhaustive recall.
    """
    if target_words < 1:
        raise ValueError('Extraction target words must be positive')
    glossary = packet_definitions(packets, document_id) if abbreviation_glossary is None else abbreviation_glossary
    targets, seen = [], set()
    for packet in packets:
        fresh = []
        for number, sentence in enumerate(packet['sentences'], 1):
            identifier = sentence_identity(sentence, packet)
            if identifier not in seen:
                fresh.append((number, sentence, identifier))
                seen.add(identifier)

        # Keep complete parsed paragraphs together where they fit. Missing
        # paragraph metadata falls back to individual sentences, without a
        # scientific relevance heuristic.
        paragraphs = []
        for row in fresh:
            number, sentence, _ = row
            key = (sentence.get('chapter_id', packet.get('chapter_id')),
                   sentence.get('section_heading', packet['section_heading']),
                   sentence.get('paragraph_id') or f'sentence-{number}')
            if (paragraphs and paragraphs[-1][0] == key
                    and paragraphs[-1][1][-1][0] + 1 == number):
                paragraphs[-1][1].append(row)
            else:
                paragraphs.append((key, [row]))

        groups, current, size, chapter = [], [], 0, None
        for key, paragraph in paragraphs:
            paragraph_size = sum(word_count(row[1]['text']) for row in paragraph)
            if current and (key[0] != chapter or size + paragraph_size > target_words
                            or current[-1][0] + 1 != paragraph[0][0]):
                groups.append(current)
                current, size = [], 0
            chapter = key[0]
            for row in paragraph:
                words = word_count(row[1]['text'])
                if current and size + words > target_words:
                    groups.append(current)
                    current, size = [], 0
                current.append(row)
                size += words
        if current:
            groups.append(current)

        for group in groups:
            target = deepcopy(packet)
            target.update(target_id='target-' + stable_hash('|'.join(row[2] for row in group), 24),
                          target_number=len(targets) + 1,
                          target_sentence_numbers=[row[0] for row in group],
                          target_source_sentence_ids=[row[2] for row in group],
                          target_word_count=sum(word_count(row[1]['text']) for row in group),
                          target_page_start=group[0][1]['page'], target_page_end=group[-1][1]['page'],
                          targeting_version=TARGETING_VERSION)
            target['reading_context_sentence_numbers'] = reading_context_numbers(target)
            target['reading_context_policy'] = READING_CONTEXT_POLICY
            target['reading_context_word_count'] = sum(word_count(target['sentences'][n - 1]['text'])
                for n in target['reading_context_sentence_numbers'])
            target['source_abbreviation_definitions'] = relevant_definitions({
                'source_document_id': document_id,
                'source_evidence_span': ' '.join(row[1]['text'] for row in group)}, glossary)
            targets.append(target)
    return targets


def reading_context_numbers(packet):
    """Select nearby complete sentences within a shared hard context budget.

    Consider touched and at most two adjacent paragraphs per side, same chapter.
    Expand nearest-first, with left-side priority on ties for antecedents. Stop
    a side at a sentence that cannot fit; never skip it to fetch remote text.
    """
    selected = set(packet['target_sentence_numbers'])
    paragraphs = []
    for number, sentence in enumerate(packet['sentences'], 1):
        key = (sentence.get('chapter_id', packet.get('chapter_id')),
               sentence.get('section_heading', packet['section_heading']),
               sentence.get('paragraph_id') or f'sentence-{number}')
        if paragraphs and paragraphs[-1][0] == key:
            paragraphs[-1][1].append(number)
        else:
            paragraphs.append((key, [number]))
    touched = [i for i, (_, numbers) in enumerate(paragraphs) if selected.intersection(numbers)]
    if not touched:
        return []
    left, right = min(touched), max(touched)
    chapter = paragraphs[left][0][0]
    eligible = set()
    for index in range(max(0, left - NEIGHBOR_PARAGRAPHS), min(len(paragraphs), right + NEIGHBOR_PARAGRAPHS + 1)):
        if paragraphs[index][0][0] == chapter:
            eligible.update(paragraphs[index][1])
    context, words, blocked = set(), 0, set()
    boundaries = {'left': min(selected) - 1, 'right': max(selected) + 1}
    while len(blocked) < 2:
        choices = [(min(selected) - n if side == 'left' else n - max(selected), side != 'left', side, n)
                   for side, n in boundaries.items() if side not in blocked]
        _, _, side, number = min(choices)
        if number not in eligible or number in selected:
            blocked.add(side)
            continue
        size = word_count(packet['sentences'][number - 1]['text'])
        if words + size > READING_CONTEXT_WORDS:
            blocked.add(side)
            continue
        context.add(number)
        words += size
        boundaries[side] += -1 if side == 'left' else 1
    return sorted(context)


def targeted_prompt(packet, source_title, *, context_mode='local'):
    """Keep original IDs; full context is available for isolated comparisons."""
    if context_mode not in {'local', 'full'}:
        raise ValueError('Extraction reading context must be local or full')
    selected = set(packet['target_sentence_numbers'])
    context_numbers = (set(range(1, len(packet['sentences']) + 1)) - selected if context_mode == 'full'
        else set(packet.get('reading_context_sentence_numbers', reading_context_numbers(packet))))
    target_lines, context_lines = [], []
    last_heading = {True: None, False: None}
    for number, sentence in enumerate(packet['sentences'], 1):
        is_target = number in selected
        if not is_target and number not in context_numbers:
            continue
        lines = target_lines if is_target else context_lines
        heading = sentence.get('section_heading', packet['section_heading'])
        if last_heading[is_target] != heading:
            lines.append(f'[Section: {heading}]')
            last_heading[is_target] = heading
        lines.append(f"[{number}] [page {sentence['page']}] {sentence['text']}")
    definitions = [{k: row[k] for k in ('abbreviation', 'expansion', 'quote', 'page', 'packet_number',
        'sentence_number', 'ambiguous') if k in row} for row in packet.get('source_abbreviation_definitions', [])]
    definition_block = ('\n<source_definitions_only>\n' + json.dumps(definitions, ensure_ascii=False)
        + '\n</source_definitions_only>\n') if definitions else ''
    return (f"Source paper: {source_title}\nTarget ID: {packet['target_id']}\n"
            f"Target sentence IDs: {packet['target_sentence_numbers']}\n"
            f"Support-only sentence IDs (never extract separate findings): {sorted(context_numbers)}\n"
            f"Allowed evidence sentence IDs: {sorted(selected | context_numbers)}\n"
            "<extraction_target>\n" + '\n'.join(target_lines) + "\n</extraction_target>\n"
            "<reading_context_only>\n" + '\n'.join(context_lines) + "\n</reading_context_only>\n"
            + definition_block +
            "First identify the target sentence stating each finding. Extract findings from extraction_target and cite at least one target sentence. Never append an unrelated target ID just to satisfy this rule. Supplied context IDs may be cited as supporting evidence. Other sentences and their headings "
            "can resolve explicit referents, crop identity, modality and restrictions; do not extract their "
            "separate findings. Context is selected, not the complete paper; missing sentence IDs are not supplied. "
            "Source definitions explain abbreviations only; they do not establish another finding or ontology identity. "
            "Context is bounded to complete sentences; if a necessary antecedent or condition is missing, omit the claim rather than guess. Do not guess from the title or project scope. "
            "Use original sentence IDs, never renumber the blocks.")
