"""Assign bounded extraction regions without an LLM discovery/filtering pass."""

from copy import deepcopy

from backend.shared.storage import stable_hash
from backend.stages.pdf_parsing.packets import word_count


TARGETING_VERSION = 2


def check_target_evidence(response, allowed_numbers):
    from backend.providers.structured_output import SchemaValidationError

    allowed = set(allowed_numbers)
    for index, claim in enumerate(response['claims']):
        numbers = claim.get('evidence_sentence_numbers') or []
        if (not 1 <= len(numbers) <= 3 or any(type(number) is not int for number in numbers)
                or not set(numbers) <= allowed
                or numbers != sorted(set(numbers))):
            raise SchemaValidationError(f'claims.{index}.evidence_sentence_numbers={numbers!r}: '
                f'use 1-3 unique, ascending sentence IDs inside the extraction target; allowed IDs={sorted(allowed)}. '
                'Non-adjacent IDs are permitted; their gaps remain explicit for source review. Do not renumber sentences.')


def sentence_identity(sentence, packet):
    """Identify the same parsed occurrence across overlapping context packets."""
    return sentence.get('source_sentence_id') or stable_hash('|'.join(str(value) for value in (
        sentence.get('chapter_id', packet.get('chapter_id', 'chapter-1')),
        sentence.get('page'), sentence.get('section_heading', packet['section_heading']),
        sentence.get('paragraph_id'), sentence.get('page_sentence_index'), sentence['text'],
    )), 32)


def build_extraction_targets(packets, target_words=800):
    """Keep original reading packets; assign each distinct occurrence once.

    Targets prefer paragraph boundaries and never cross chapters. Multiple
    nearby headings may share a target; each sentence retains its own heading.
    Oversized paragraphs split at sentence boundaries. A single long sentence
    remains intact. Every parsed sentence is assigned, including those without
    domain keywords. Processing coverage is not a claim of exhaustive recall.
    """
    if target_words < 1:
        raise ValueError('Extraction target words must be positive')
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
            targets.append(target)
    return targets


def targeted_prompt(packet, source_title):
    """Separate extraction evidence and reading-only context, keeping source IDs."""
    selected = set(packet['target_sentence_numbers'])
    target_lines, context_lines = [], []
    last_heading = {True: None, False: None}
    for number, sentence in enumerate(packet['sentences'], 1):
        is_target = number in selected
        lines = target_lines if is_target else context_lines
        heading = sentence.get('section_heading', packet['section_heading'])
        if last_heading[is_target] != heading:
            lines.append(f'[Section: {heading}]')
            last_heading[is_target] = heading
        lines.append(f"[{number}] [page {sentence['page']}] {sentence['text']}")
    return (f"Source paper: {source_title}\nTarget ID: {packet['target_id']}\n"
            f"Allowed evidence sentence IDs: {packet['target_sentence_numbers']}\n"
            "<extraction_target>\n" + '\n'.join(target_lines) + "\n</extraction_target>\n"
            "<reading_context_only>\n" + '\n'.join(context_lines) + "\n</reading_context_only>\n"
            "Extract only direct edges evidenced within extraction_target. Other sentences and their headings "
            "can resolve explicit referents, crop identity, modality and restrictions; do not extract their "
            "separate findings. Use original sentence IDs, never renumber the blocks.")
