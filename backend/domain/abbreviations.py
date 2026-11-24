"""Source-defined abbreviation hints, never a dictionary of scientific identities."""
import re
from copy import deepcopy

from backend.shared.storage import stable_hash
from backend.domain.alignment import exact_in

VERSION = 'source-defined-abbreviations-v5'
PARENTHESIS = re.compile(r'\(([^()\n]{1,100})\)')
SHORT = re.compile(r'[A-Za-z][A-Za-z0-9-]{0,14}\Z')
SCIENTIFIC_SHORT = r'[A-Z][a-z]?\s*\.\s*[a-z][a-z0-9-]{1,39}'
BINOMIAL_SHORT_RE = re.compile(r'(?<!\w)([A-Z])\s*\.\s*([a-z][a-z0-9-]{2,39})(?!\w)')
FULL_BINOMIAL_RE = re.compile(r'(?<!\w)([A-Z][a-z]{2,39})\s+([a-z][a-z0-9-]{2,39})(?!\w)')
_NON_BINOMIAL_WORDS = frozenset({
    'sp', 'spp', 'var', 'subsp', 'cv', 'cf', 'et', 'al', 'vs', 'fig', 'tab', 'eq',
    'and', 'or', 'of', 'in', 'to', 'by', 'at', 'as', 'is', 'are', 'was', 'were',
    'the', 'for', 'with', 'from', 'on', 'that', 'this', 'under', 'over', 'into',
    'than', 'per', 'mg', 'kg', 'ha', 'ml', 'nm', 'mm', 'cm',
})
BEFORE_SHORT = re.compile(r'(' + SCIENTIFIC_SHORT + r'|[A-Za-z][A-Za-z0-9-]{0,14})\s*$')
NAME_FIELDS = ('subject', 'object', 'relation', 'claim_text', 'subject_context', 'object_context',
    'qualifiers.crop_or_species', 'qualifiers.population', 'qualifiers.condition',
    'qualifiers.comparator', 'qualifiers.metric')


def _short(text):
    return bool((SHORT.fullmatch(text) or re.fullmatch(SCIENTIFIC_SHORT, text))
        and any(c.isupper() for c in text))


def alias_pattern(short):
    # Permit typesetting spaces around a genus-initial period (e.g. "P. mucilaginosus"
    # or "P . mucilaginosus"), while preventing standalone single/short aliases such as
    # "P" from matching the genus initial of a binomial name.
    escaped = re.escape(short)
    if re.fullmatch(SCIENTIFIC_SHORT, short):
        parts = re.split(r'\s*\.\s*', short, maxsplit=1)
        escaped = re.escape(parts[0]) + r'\s*\.\s*' + re.escape(parts[1])
        return re.compile(r'(?<!\w)' + escaped + r'(?!\w)')
    suffix_guard = r'(?!\s*\.\s*[a-z][a-z0-9-]{1,39})' if not short.endswith('.') else ''
    return re.compile(r'(?<!\w)' + escaped + suffix_guard + r'(?!\w)')


_FINITE_CLAUSE_VERB_RE = re.compile(
    r'\b(?:is|are|was|were|had|has|have|did|does|do|showed|indicated|revealed|observed)\b',
    re.I,
)


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
            or _FINITE_CLAUSE_VERB_RE.search(long)
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


def _binomial_pair_valid(short, full, quote):
    """True when `short` abbreviates the genus initial of a verbatim document binomial."""
    if ' '.join(str(full or '').split()).casefold() != ' '.join(str(quote or '').split()).casefold():
        return False
    m_full = FULL_BINOMIAL_RE.fullmatch(' '.join(str(quote or '').split()))
    m_short = BINOMIAL_SHORT_RE.fullmatch(' '.join(str(short or '').split()))
    if not (m_full and m_short):
        return False
    genus, species = m_full[1], m_full[2]
    initial, short_species = m_short[1], m_short[2]
    return (species.casefold() not in _NON_BINOMIAL_WORDS
            and genus[0] == initial and species == short_species)


def _binomial_definitions(text_entries, document, seen_keys):
    """Collect document-scoped `G. species` expansions only when `Genus species` appears unambiguously."""
    short_mentions = set()
    full_by_key = {}
    for text, meta in text_entries:
        if not text:
            continue
        for m in BINOMIAL_SHORT_RE.finditer(text):
            if m[2].casefold() not in _NON_BINOMIAL_WORDS:
                short_mentions.add((m[1], m[2]))
        for m in FULL_BINOMIAL_RE.finditer(text):
            genus, species = m[1], m[2]
            if species.casefold() in _NON_BINOMIAL_WORDS or genus.casefold() in _NON_BINOMIAL_WORDS:
                continue
            quote = f'{genus} {species}'
            key = (genus[0], species)
            bucket = full_by_key.setdefault(key, {})
            if quote not in bucket:
                bucket[quote] = dict(meta)
    rows = []
    for key in sorted(short_mentions):
        candidates = full_by_key.get(key) or {}
        if len(candidates) != 1:
            continue
        quote, meta = next(iter(candidates.items()))
        short = f'{key[0]}. {key[1]}'
        dedup_key = (document, short, quote, quote)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        rows.append({'abbreviation': short, 'expansion': quote, 'quote': quote,
                     'document': document, **meta})
    return rows


def packet_definitions(packets, document):
    """Collect once from parsed sentences, including ones with no extracted claim."""
    rows, seen, binomial_entries = [], set(), []
    for packet in packets:
        for number, sentence in enumerate(packet['sentences'], 1):
            text = sentence['text']
            meta = {'context_field': 'parsed_sentence',
                    'packet_number': packet.get('packet_number'), 'sentence_number': number,
                    'page': sentence.get('page'), 'context_sha256': stable_hash(text, 64)}
            binomial_entries.append((text, meta))
            for row in definitions(text):
                key = (document, row['abbreviation'], row['expansion'], row['quote'])
                if key in seen:
                    continue
                seen.add(key)
                rows.append({**row, 'document': document, **meta})
    rows.extend(_binomial_definitions(binomial_entries, document, seen))
    return rows


def collect_definitions(claims):
    found, seen, contexts = [], set(), set()
    entries_by_doc = {}
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
            meta = {'candidate_id': claim.get('candidate_id'), 'context_field': field,
                    'context_sha256': fingerprint,
                    'page': claim.get('source_page') if field == 'source_evidence_span' else None}
            entries_by_doc.setdefault(document, []).append((text, meta))
            for row in definitions(text):
                key = (document, row['abbreviation'], row['expansion'], row['quote'])
                if key in seen:
                    continue
                seen.add(key)
                found.append({**row, 'document': document, **meta})
    for document, entries in entries_by_doc.items():
        found.extend(_binomial_definitions(entries, document, seen))
    return found


_BROKEN_HYPHEN_RE = re.compile(r'(?<=[A-Za-z]{2})\s+-\s+(?=[A-Za-z]{2})')
_UNICODE_HYPHENS = str.maketrans({'\u2010': '-', '\u2011': '-', '\u2012': '-', '\u2013': '-', '\u2014': '-', '\u2212': '-'})


def _clean_expansion(text):
    return _BROKEN_HYPHEN_RE.sub('', str(text or '').translate(_UNICODE_HYPHENS))


def relevant_definitions(claim, glossary):
    doc = document_key(claim)
    text = ' '.join(str(claim.get(k) or '') for k in ('subject', 'relation', 'object', 'qualifiers', 'source_evidence_span'))
    doc_rows = [row for row in glossary if row['document'] == doc]
    selected = [row for row in doc_rows if alias_pattern(row['abbreviation']).search(text)]
    seen = {id(row) for row in selected}
    for _ in range(4):
        expansions_text = ' '.join(str(row.get('expansion') or '') for row in selected)
        added = [row for row in doc_rows if id(row) not in seen and alias_pattern(row['abbreviation']).search(expansions_text)]
        if not added:
            break
        selected.extend(added)
        seen.update(id(row) for row in added)
    rows = [row for row in doc_rows if id(row) in seen]
    _, ambiguous = resolved_aliases(glossary, doc)
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
            or short == full or not exact_in(quote, context) or not exact_in(full, quote)):
        return False
    if not exact_in(short, quote) and not _binomial_pair_valid(short, full, quote):
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
        is_parenthetical = any(d['abbreviation'] == short and ' '.join(d['expansion'].split()).casefold()
                == ' '.join(full.split()).casefold() for d in definitions(quote))
        if not (is_parenthetical or _binomial_pair_valid(short, full, quote)):
            continue
        groups.setdefault(short, []).append(row)
    resolved = {}
    def canonical(text, exclude):
        text = _clean_expansion(text)
        for _ in range(8):
            original = text
            for key, row in sorted(resolved.items(), key=lambda item: -len(item[0])):
                if key != exclude:
                    text = alias_pattern(key).sub(lambda _: _clean_expansion(' '.join(row['expansion'].split())), text)
            if text == original:
                break
        return ' '.join(_clean_expansion(text).split()).casefold()
    for _ in range(min(16, len(groups) + 1)):
        before = dict(resolved)
        for short, rows in groups.items():
            if len({canonical(row['expansion'], short) for row in rows}) == 1:
                # Prefer a clean source form already spelled out over one with
                # broken line hyphens or another known abbreviation; no rewording.
                resolved[short] = min(
                    rows,
                    key=lambda r: (
                        int(bool(_BROKEN_HYPHEN_RE.search(r['expansion']))),
                        sum(bool(alias_pattern(k).search(r['expansion'])) for k in groups if k != short),
                    ),
                )
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
