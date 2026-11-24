"""Assign bounded extraction regions without an LLM discovery/filtering pass."""

from copy import deepcopy
import json
import re

from backend.shared.storage import stable_hash
from backend.stages.pdf_parsing.packets import word_count
from backend.domain.abbreviations import packet_definitions, relevant_definitions


TARGETING_VERSION = 6
READING_CONTEXT_POLICY = 'bounded-sentence-supporting-evidence-v6'
READING_CONTEXT_WORDS = 250  # Hard context allowance; source sentences are never cut.
NEIGHBOR_PARAGRAPHS = 2
MAX_METHODS_CONTEXT_SENTENCES = 2
MAX_METHODS_CONTEXT_WORDS = 80
MAX_CAPTION_CONTEXT_SENTENCES = 2
MAX_CAPTION_CONTEXT_WORDS = 80

_METHODS_HEADING_RE = re.compile(
    r'\b(?:materials?\s+and\s+methods?|methods?|mineral\s+analys[ei]s|'
    r'experimental\s+design|statistical\s+analys[ei]s|data\s+analys[ei]s)\b',
    re.I,
)
_MEASURED_TARGET_RE = re.compile(
    r'(?:\b(?:mg/g|g/kg|ppm|mineral\w*|macromineral\w*|micromineral\w*|'
    r'nutrient\w*|content\w*|concentration\w*|level\w*)\b|[µμu]g/g|%)',
    re.I,
)
# "Table 2" / "Figure 3" in running text. A supplementary item lives outside this paper's caption set.
_VISUAL_REFERENCE_RE = re.compile(
    r'(?<![Ss]upplementary\s)(?<![Ss]upplemental\s)\b(Tables?|Figures?|Figs?\.?)\s*(\d{1,2})\b')
_METHODS_BASIS_RE = re.compile(
    r'\b(?:expressed\s+(?:in\s+terms\s+of|as)|'
    r'on\s+a\s+(?:[\w-]+\s+){0,3}(?:dry|fresh)[-\s]weight\s*(?:\([A-Z]{2,4}\)\s*)?basis|'
    r'determined\s+on\s+a\s+(?:dry|fresh)[-\s]weight)\b',
    re.I,
)
_METHODS_ENV_RE = re.compile(
    r'\b(?:environments?\s*\(considered\s+as\s+location[-\s]year|'
    r'evaluated\s+in\s+(?:three|two|\d+)\s+environments?|raw\s+potato\s+tubers?)\b',
    re.I,
)
_SITE_YEAR_RE = re.compile(r'\b(?:19|20)\d{2}\b')


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
            _attach_remote_captions(target, packets)
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
    local_selected = sorted(context)
    methods_selected = _methods_context_numbers(packet, packet['target_sentence_numbers'], local_selected)
    caption_selected = _caption_context_numbers(packet, packet['target_sentence_numbers'],
                                                set(local_selected) | set(methods_selected))
    return sorted(set(local_selected) | set(methods_selected) | set(caption_selected))


def _visual_references(sentences, numbers):
    """Ordered (kind, label) of tables/figures named in the given sentences."""
    wanted = []
    for n in numbers:
        for kind, label in _VISUAL_REFERENCE_RE.findall(str(sentences[n - 1].get('text') or '')):
            key = ('table' if kind.lower().startswith('table') else 'figure', label)
            if key not in wanted:
                wanted.append(key)
    return wanted


def _caption_key(text):
    head = re.match(r'\s*(TABLE|Table|FIGURE|Figure|FIG\.?|Fig\.?)\s*(\d{1,2})\b', str(text or ''))
    if not head:
        return None
    return ('table' if head.group(1).lower().startswith('table') else 'figure', head.group(2))


def _caption_context_numbers(packet, target_numbers, already_selected):
    """Supply the caption of a table or figure that a target sentence explicitly cites.

    A result sentence such as "...between market groups (Table 2)" inherits the table's
    environments, years and populations from its caption, not from the nearest prose sentence.
    Only captions of items named in the target are added, within a small word budget, so the
    model can scope a qualifier to the evidence the paper actually gives.
    """
    sentences = packet.get('sentences') or []
    if not target_numbers or not sentences:
        return []
    in_range = [n for n in target_numbers if 1 <= n <= len(sentences)]
    wanted = _visual_references(sentences, in_range)
    if not wanted:
        return []
    chapters = {sentences[n - 1].get('chapter_id', packet.get('chapter_id')) for n in in_range}
    excluded = set(target_numbers) | set(already_selected)
    found = {}
    for number, sentence in enumerate(sentences, 1):
        if number in excluded or sentence.get('chapter_id', packet.get('chapter_id')) not in chapters:
            continue
        key = _caption_key(sentence.get('text'))
        if key in wanted and key not in found:
            found[key] = number
    selected, words = [], 0
    for key in wanted:
        number = found.get(key)
        if number is None:
            continue
        size = word_count(sentences[number - 1]['text'])
        if words + size > MAX_CAPTION_CONTEXT_WORDS:
            continue
        selected.append(number)
        words += size
        if len(selected) >= MAX_CAPTION_CONTEXT_SENTENCES:
            break
    return sorted(selected)


def _attach_remote_captions(target, packets):
    """Append a cited item's caption when page layout placed it in a different context packet.

    The copy is added after the last parsed sentence so numbering of every real sentence is
    unchanged. It is marked with its origin, is never a target, and may be cited as support.
    """
    sentences = target.get('sentences') or []
    in_range = [n for n in target.get('target_sentence_numbers', []) if 1 <= n <= len(sentences)]
    wanted = _visual_references(sentences, in_range)
    if not wanted:
        return
    present = {_caption_key(sentences[n - 1].get('text')) for n in
               set(target['reading_context_sentence_numbers']) | set(in_range)}
    used = sum(1 for n in target['reading_context_sentence_numbers']
               if _caption_key(sentences[n - 1].get('text')))
    words = sum(word_count(sentences[n - 1]['text']) for n in target['reading_context_sentence_numbers']
                if _caption_key(sentences[n - 1].get('text')))
    chapters = {sentences[n - 1].get('chapter_id', target.get('chapter_id')) for n in in_range}
    for key in wanted:
        if key in present or used >= MAX_CAPTION_CONTEXT_SENTENCES:
            continue
        for other in packets:
            if other.get('packet_number') == target.get('packet_number'):
                continue
            match = next(((n, s) for n, s in enumerate(other['sentences'], 1)
                          if _caption_key(s.get('text')) == key
                          and s.get('chapter_id', other.get('chapter_id')) in chapters), None)
            if match is None:
                continue
            origin_number, original = match
            size = word_count(original['text'])
            if words + size > MAX_CAPTION_CONTEXT_WORDS:
                break
            appended = dict(original)
            appended['appended_context'] = {'kind': 'caption_from_other_packet',
                                            'origin_packet_number': other.get('packet_number'),
                                            'origin_sentence_number': origin_number,
                                            'origin_source_sentence_id': sentence_identity(original, other)}
            appended.setdefault('chapter_id', other.get('chapter_id'))
            appended.setdefault('section_heading', other.get('section_heading'))
            sentences.append(appended)
            target['reading_context_sentence_numbers'] = sorted(
                set(target['reading_context_sentence_numbers']) | {len(sentences)})
            used += 1
            words += size
            present.add(key)
            break


def _methods_context_numbers(packet, target_numbers, local_selected):
    """Add bounded Methods basis/design sentences for quantitative non-Methods targets."""
    sentences = packet.get('sentences') or []
    if not target_numbers or not sentences:
        return []
    if any(_METHODS_HEADING_RE.search(str(sentences[n - 1].get('section_heading') or packet.get('section_heading') or ''))
           for n in target_numbers if 1 <= n <= len(sentences)):
        return []
    target_text = ' '.join(str(sentences[n - 1].get('text') or '') for n in target_numbers if 1 <= n <= len(sentences))
    if not _MEASURED_TARGET_RE.search(target_text):
        return []
    chapters = {sentences[n - 1].get('chapter_id', packet.get('chapter_id'))
                for n in target_numbers if 1 <= n <= len(sentences)}
    excluded = set(target_numbers) | set(local_selected)
    local_text = target_text + ' ' + ' '.join(
        str(sentences[n - 1].get('text') or '') for n in local_selected if 1 <= n <= len(sentences))
    needs_env = not _SITE_YEAR_RE.search(local_text)

    basis_num = None
    env_num = None
    for number, sentence in enumerate(sentences, 1):
        if number in excluded:
            continue
        if sentence.get('chapter_id', packet.get('chapter_id')) not in chapters:
            continue
        heading = str(sentence.get('section_heading') or packet.get('section_heading') or '')
        if not _METHODS_HEADING_RE.search(heading):
            continue
        text = str(sentence.get('text') or '')
        if basis_num is None and _METHODS_BASIS_RE.search(text):
            basis_num = number
        if needs_env and env_num is None and number != basis_num and _METHODS_ENV_RE.search(text):
            env_num = number

    selected = []
    words = 0
    for number in (basis_num, env_num):
        if number is None or number in selected:
            continue
        size = word_count(sentences[number - 1]['text'])
        if words + size > MAX_METHODS_CONTEXT_WORDS:
            continue
        selected.append(number)
        words += size
        if len(selected) >= MAX_METHODS_CONTEXT_SENTENCES:
            break
    return sorted(selected)


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
            "Use original sentence IDs, never renumber the blocks, and always list evidence_sentence_ids as unique integers in strictly ascending numerical order (e.g. [70, 122, 124], never [124, 70]). Cite every supplied sentence that establishes a condition, year/site-year, organ/tissue or weight basis in qualifiers. A qualifier must describe the cited finding itself: never copy a year, site or treatment from a neighbouring sentence that reports a different analysis. When the finding comes from a table or figure whose caption is supplied, take its environments and years from that caption and cite the caption ID; omit a year or site that no cited sentence establishes.")
