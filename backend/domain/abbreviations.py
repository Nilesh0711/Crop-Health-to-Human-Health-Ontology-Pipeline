"""Source-defined abbreviation hints, never a dictionary of scientific identities."""
import re
from copy import deepcopy

from backend.shared.storage import stable_hash
from backend.domain.alignment import exact_in

VERSION = 'source-defined-abbreviations-v4'
PARENTHESIS = re.compile(r'\(([^()\n]{1,100})\)')
SHORT = re.compile(r'[A-Za-z][A-Za-z0-9-]{0,14}\Z')
SCIENTIFIC_SHORT = r'[A-Z][a-z]?\.\s*[a-z][a-z0-9-]{1,39}'
BEFORE_SHORT = re.compile(r'(' + SCIENTIFIC_SHORT + r'|[A-Za-z][A-Za-z0-9-]{0,14})\s*$')
NAME_FIELDS = ('subject', 'object', 'relation', 'claim_text', 'subject_context', 'object_context',
    'qualifiers.crop_or_species', 'qualifiers.population', 'qualifiers.condition',
    'qualifiers.comparator', 'qualifiers.metric')


def _short(text):
    return bool((SHORT.fullmatch(text) or re.fullmatch(SCIENTIFIC_SHORT, text))
        and any(c.isupper() for c in text))


def alias_pattern(short):
    # Permit typesetting spaces after a genus initial, but preserve case and
    # token boundaries so treatment/gene identifiers are not substring-edited.
    escaped = re.escape(short)
    if re.fullmatch(SCIENTIFIC_SHORT, short):
        escaped = re.sub(r'(?:\\ )+', r'\\s*', escaped)
        if r'\s*' not in escaped:
            escaped = escaped.replace(r'\.', r'\.\s*', 1)
    return re.compile(r'(?<!\w)' + escaped + r'(?!\w)')


def _long_form(short, preceding):
    """Align abbreviation characters backwards to a bounded literal long form."""
    letters = ''.join(c for c in short if c.isalnum())
    cursor = len(preceding) - 1
    for index in range(len(letters) - 1, -1, -1):
        while cursor >= 0:
            if preceding[cursor].casefold() == letters[index].casefold():
                if index or cursor == 0 or not preceding[cursor - 1].isalnum():
                    break
            cursor -= 1
        if cursor < 0:
            return None
        cursor -= 1
    long = preceding[cursor + 1:].strip()
    if (len(long) <= len(short) or len(long.split()) > min(len(letters) + 5, len(letters) * 2)
            or len(letters) == 1 and (len(long.split()) != 1 or len(long) < 3)
            or alias_pattern(short).search(long)):
        return None
    return long


def definitions(text):
    for match in PARENTHESIS.finditer(text):
        inside = match[1].strip()
        prefix_start = max(0, match.start() - 220)
        preceding = text[prefix_start:match.start()].rstrip()
        if _short(inside):
            long = _long_form(inside, preceding)
            if long:
                yield {'abbreviation': inside, 'expansion': long,
                       'quote': text[prefix_start + preceding.rfind(long):match.end()]}
        else:
            before = BEFORE_SHORT.search(preceding)
            if before and _short(before[1]) and _long_form(before[1], inside) == inside:
                yield {'abbreviation': before[1], 'expansion': inside,
                       'quote': text[prefix_start + before.start():match.end()]}


def document_key(claim):
    return str(claim.get('source_document_id') or claim.get('source_paper_title') or 'current-document')


def packet_definitions(packets, document):
    """Collect once from parsed sentences, including ones with no extracted claim."""
    rows, seen = [], set()
    for packet in packets:
        for number, sentence in enumerate(packet['sentences'], 1):
            text = sentence['text']
            for row in definitions(text):
                key = (row['abbreviation'], row['expansion'], row['quote'])
                if key in seen:
                    continue
                seen.add(key)
                rows.append({**row, 'document': document, 'context_field': 'parsed_sentence',
                    'packet_number': packet.get('packet_number'), 'sentence_number': number,
                    'page': sentence.get('page'), 'context_sha256': stable_hash(text, 64)})
    return rows


def collect_definitions(claims):
    found, seen, contexts = [], set(), set()
    for claim in claims:
        document = document_key(claim)
        for inherited in claim.get('source_abbreviation_definitions') or []:
            if inherited.get('document') == document and inherited.get('quote'):
                key = (document, inherited['abbreviation'], inherited['expansion'], inherited['quote'])
                if key not in seen:
                    seen.add(key)
                    found.append(dict(inherited))
        for field in ('source_evidence_span', 'source_local_context', 'source_reading_context', 'source_context_packet'):
            text = claim.get(field) or ''
            fingerprint = stable_hash(text, 64)
            context_key = (document, field, fingerprint)
            if context_key in contexts:
                continue
            contexts.add(context_key)
            for row in definitions(text):
                key = (document, row['abbreviation'], row['expansion'], row['quote'])
                if key in seen:
                    continue
                seen.add(key)
                found.append({**row, 'document': document, 'candidate_id': claim.get('candidate_id'),
                    'context_field': field, 'context_sha256': fingerprint,
                    'page': claim.get('source_page') if field == 'source_evidence_span' else None})
    return found


def relevant_definitions(claim, glossary):
    text = ' '.join(str(claim.get(k) or '') for k in ('subject', 'relation', 'object', 'qualifiers', 'source_evidence_span'))
    rows = [row for row in glossary if row['document'] == document_key(claim)
        and alias_pattern(row['abbreviation']).search(text)]
    _, ambiguous = resolved_aliases(glossary, document_key(claim))
    return [{**row, 'ambiguous': row['abbreviation'] in ambiguous} for row in rows]


def name_replacement_matches(before, after, short, full):
    """The edit may substitute a bounded alias, and may change nothing else."""
    expected, count = replace_alias(before, short, full)
    return bool(count and expected == after)


def replace_alias(text, short, full):
    """Preserve degree-unit symbols even if a paper defines the same letter."""
    count = 0
    def substitute(match):
        nonlocal count
        if re.search(r'\u00b0\s*$', text[:match.start()]):
            return match[0]
        count += 1
        return full
    return alias_pattern(short).sub(substitute, text), count


def compact_expanded_parentheses(text):
    """Remove only a parenthesis that repeats its immediately preceding name."""
    for match in reversed(list(re.finditer(r'\(([^()]+)\)', text))):
        full = match[1].strip()
        prefix = text[:match.start()].rstrip()
        start = len(prefix) - len(full)
        if (start >= 0 and prefix[start:].casefold() == full.casefold()
                and (start == 0 or not prefix[start - 1].isalnum())):
            text = prefix + text[match.end():]
    return text


def verify_name_expansion(before, after, short, full, quote, context):
    """Shared edit/provenance guard; this is not a scientific-fidelity verdict."""
    if (not all(isinstance(v, str) and v.strip() for v in (before, after, short, full, quote))
            or short == full or not exact_in(quote, context)
            or not exact_in(short, quote) or not exact_in(full, quote)):
        return False
    return name_replacement_matches(before, after, short, full)


def resolved_aliases(glossary, document):
    """Choose only source-paired definitions that agree after other clear aliases.

    No most-common/last-definition heuristic. Nested P in P-use efficiency can
    be resolved when P has its own explicit definition. Conflicting meanings
    remain unresolved. No chemical forms or ontology identities are inferred.
    """
    groups = {}
    for row in glossary:
        if row.get('document') != document:
            continue
        short, full, quote = (row.get(k, '') for k in ('abbreviation', 'expansion', 'quote'))
        if not _short(short):
            continue
        if not any(d['abbreviation'] == short and ' '.join(d['expansion'].split()).casefold()
                == ' '.join(full.split()).casefold() for d in definitions(quote)):
            continue
        groups.setdefault(short, []).append(row)
    resolved = {}
    def canonical(text, exclude):
        for _ in range(8):
            original = text
            for key, row in sorted(resolved.items(), key=lambda item: -len(item[0])):
                if key != exclude:
                    text = alias_pattern(key).sub(lambda _: ' '.join(row['expansion'].split()), text)
            if text == original:
                break
        return ' '.join(text.split()).casefold()
    for _ in range(min(16, len(groups) + 1)):
        before = dict(resolved)
        for short, rows in groups.items():
            if len({canonical(row['expansion'], short) for row in rows}) == 1:
                # Prefer a source form already spelled out over one containing
                # another known abbreviation; no scientific rewording is made.
                resolved[short] = min(rows, key=lambda r: sum(bool(alias_pattern(k).search(r['expansion']))
                    for k in groups if k != short))
            else:
                resolved.pop(short, None)
        if resolved == before:
            break
    return resolved, set(groups) - resolved.keys()


def abbreviation_source_anchor(proposed, evidence, glossary, document):
    """Return a literal selected-evidence span, never a guessed expansion."""
    from backend.shared.text import source_surface_mention
    aliases, _ = resolved_aliases(glossary, document)
    ambiguous = {r.get('abbreviation') for r in glossary if r.get('document') == document and r.get('ambiguous')}
    aliases = {k: v for k, v in aliases.items() if k not in ambiguous}
    expanded = proposed
    used = []
    for short, row in sorted(aliases.items(), key=lambda item: -len(item[0])):
        expanded, count = replace_alias(expanded, short, row['expansion'])
        if count:
            used.append(row)
    variants = [(expanded, used)] if expanded != proposed else []
    for short, row in aliases.items():
        pattern = re.compile(r'(?<!\w)' + re.escape(row['expansion']) + r'(?!\w)', re.I)
        value, count = pattern.subn(lambda _: short, expanded)
        if count:
            variants.append((value, [*used, row]))
    contracted = expanded
    contracted_definitions = list(used)
    for short, row in sorted(aliases.items(), key=lambda item: -len(item[1]['expansion'])):
        pattern = re.compile(r'(?<!\w)' + re.escape(row['expansion']) + r'(?!\w)', re.I)
        contracted, count = pattern.subn(lambda _: short, contracted)
        if count:
            contracted_definitions.append(row)
    if contracted != expanded:
        variants.append((contracted, contracted_definitions))
    matches = {}
    for value, definitions_used in variants:
        span = source_surface_mention(evidence, value)
        if span and any(span == short and len(short) == 1 for short in aliases):
            # A letter after a degree symbol is a unit, not a document alias.
            occurrences = list(re.finditer(r'(?<!\w)' + re.escape(span) + r'(?!\w)', evidence))
            if not any(not re.search(r'\u00b0\s*$', evidence[:match.start()]) for match in occurrences):
                continue
        if span:
            matches[span] = definitions_used
    if len(matches) != 1:
        return None
    span, definitions_used = next(iter(matches.items()))
    return span, [{'abbreviation': r['abbreviation'], 'expansion': r['expansion'],
                   'quote': r['quote'], 'document': r['document']} for r in definitions_used]


def expand_claim_names(claim, glossary=None):
    """Use full names in claim fields; evidence, IDs and source mentions stay raw.

    Also used as a read projection for older records, without modifying the
    saved model assessments or their signatures.
    """
    glossary = collect_definitions([claim]) if glossary is None else glossary
    aliases, ambiguous = resolved_aliases(glossary, document_key(claim))
    result = deepcopy(claim)
    edits = []
    source_context = '\n'.join(str(claim.get(k) or '') for k in
        ('source_evidence_span', 'source_local_context', 'source_reading_context', 'source_context_packet'))
    # These are source fragments recorded by the parser, not model-written
    # definitions; they can originate outside this claim's local paragraph.
    source_context += '\n' + '\n'.join(r['quote'] for r in glossary
        if r.get('document') == document_key(claim) and r.get('quote'))
    unresolved = set()
    for field in NAME_FIELDS:
        parent, key = (result.get('qualifiers') or {}, field.split('.', 1)[1]) if field.startswith('qualifiers.') else (result, field)
        value = parent.get(key)
        if not isinstance(value, str):
            continue
        original = value
        for _ in range(8):
            before_pass = value
            for short, row in sorted(aliases.items(), key=lambda item: -len(item[0])):
                full = ' '.join(row['expansion'].split())
                after, _ = replace_alias(value, short, full)
                if value != after and verify_name_expansion(value, after, short, full, row['quote'], source_context):
                    edits.append({'field': field, 'before': value, 'after': after, 'short_form': short,
                        'full_form': full, 'definition_quote': row['quote'], 'page': row.get('page'),
                        'rationale': 'Only the source-defined name was expanded.', 'policy': VERSION})
                    value = after
            if value == before_pass:
                break
        unresolved.update(short for short in ambiguous if alias_pattern(short).search(value))
        if value != original:
            compact = compact_expanded_parentheses(value)
            if compact != value:
                edits.append({'field': field, 'before': value, 'after': compact,
                    'policy': VERSION, 'kind': 'redundant_expanded_parentheses',
                    'rationale': 'Removed a parenthetical repetition of the expanded name only.'})
                value = compact
        parent[key] = value
        if field in ('subject', 'object') and value != original:
            result.setdefault(field + '_source_mention', original)
    if edits:
        result['source_name_corrections'] = [*result.get('source_name_corrections', []), *edits]
    if unresolved:
        result['unresolved_abbreviations'] = sorted(unresolved)
    return result
