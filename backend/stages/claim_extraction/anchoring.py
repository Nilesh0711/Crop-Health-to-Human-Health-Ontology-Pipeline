"""Bounded surface derivations, never scientific paraphrase or claim rewriting."""
import re
from itertools import combinations, product

from backend.shared.text import source_surface_mention
from backend.domain.abbreviations import abbreviation_source_anchor, alias_pattern, resolved_aliases

ANCHORING_POLICY = 'validated-endpoint-coordination-v2'
# A finite surface grammar, not a general noun-phrase or coreference parser.
# Clause verbs/prepositions cannot be swallowed by an omitted conjunct.
_BOUNDARY = (r'and|or|but|whereas|while|as|with|without|by|in|of|to|from|under|than|because|'
             r'which|that|is|are|was|were|has|have|had|can|may|might|could|would|'
             r'do|does|did|not|never|increas\w*|decreas\w*|declin\w*|rose|fell|'
             r'chang\w*|remain\w*|show\w*|enhanc\w*|reduc\w*|affect\w*|promot\w*|'
             r'caus\w*|result\w*|support\w*|stimulat\w*|inhibit\w*')
_WORD = rf'(?!(?:{_BOUNDARY})\b)[\w]+(?:[-‐‑−][\w]+)*'
_NOUN_SPAN = rf'{_WORD}(?:\s+{_WORD}){{0,5}}'
_METRIC = r'(?:\s+by\s+[<>≤≥~]?\d+(?:\.\d+)?(?:\s*[–−-]\s*\d+(?:\.\d+)?)?\s*%)?'


def _literal_pattern(words):
    return r'\s+'.join(re.escape(word) for word in words)


def coordination_spans(source, endpoint):
    """At most two explicit coordination axes within a bounded noun phrase.

    No arbitrary token gaps, cross-sentence assembly, synonyms or inserted names.
    The returned quotation is the full verbatim coordination, not a fabricated
    contiguous rendering of the atomic endpoint.
    """
    words = endpoint.split()
    if not 2 <= len(words) <= 16:
        return set()
    patterns = []
    # Reuse every endpoint word in order. Only explicit 'and' constructions
    # license omissions; no arbitrary token gaps or inserted scientific words.
    for axes in (1, 2):
        for boundaries in combinations(range(1, len(words)), axes):
            for directions in product(('suffix', 'prefix'), repeat=axes):
                gaps = dict(zip(boundaries, directions))
                parts = [re.escape(words[0])]
                for index, word in enumerate(words[1:], 1):
                    if gaps.get(index) == 'suffix':
                        parts.append(rf'\s+and\s+(?:the\s+)?{_NOUN_SPAN}\s+')
                    elif gaps.get(index) == 'prefix':
                        parts.append(rf'\s+(?:both\s+(?:the\s+)?)?{_NOUN_SPAN}{_METRIC}\s+and\s+')
                    else:
                        parts.append(r'\s+')
                    parts.append(re.escape(word))
                patterns.append(r'(?<!\w)' + ''.join(parts) + r'(?!\w)')
    for split, word in enumerate(words):
        if word.casefold() not in {'in', 'of'} or not 0 < split < len(words) - 1:
            continue
        head, item = _literal_pattern(words[:split]), _literal_pattern(words[split+1:])
        prep = re.escape(word)
        # "head in A by 12% and in B by 5%" -> "head in B".
        patterns.append(rf'(?<!\w){head}\s+{prep}\s+(?:both\s+(?:the\s+)?)?{_NOUN_SPAN}{_METRIC}\s+and\s+(?:{prep}\s+)?{item}(?!\w)')
        # "head in both A and B" -> "head in A".
        patterns.append(rf'(?<!\w){head}\s+{prep}\s+both\s+(?:the\s+)?{item}\s+and\s+(?:{prep}\s+)?{_NOUN_SPAN}(?!\w)')
    return {match.group(0) for pattern in patterns for match in re.finditer(pattern, source, re.I)}


def anchor_diagnostic(endpoint, proposed_anchor, sentences):
    """Application diagnostics only: no inferred antecedent or endpoint rewrite."""
    spans = {span for source in sentences for span in coordination_spans(source, endpoint)}
    if len(spans) > 1:
        return 'ambiguous_coordination'
    if spans and proposed_anchor and proposed_anchor != endpoint:
        return 'anchor_does_not_cover_coordination'
    if re.search(r'\b(?:it|its|their|these|those|this|that)\b', proposed_anchor or '', re.I):
        return 'unproven_antecedent'
    return 'unsupported_endpoint_surface'


_CANONICAL_STOP = frozenset(
    'a an the of in to for from by on with at under as and or is are was were its'.split()
)
_CANONICAL_HEAD_NOUNS = frozenset({
    'supplementation', 'exposure', 'treatment', 'application', 'inoculation', 'deficiency',
    'scarcity', 'shortage', 'supply', 'content', 'concentration', 'uptake', 'allocation',
    'intake', 'consumption', 'growth', 'yield', 'trait', 'activity', 'activities',
    'biosynthesis', 'level', 'levels', 'status', 'availability', 'rate', 'efficiency',
    'accumulation', 'translocation', 'composition', 'response', 'effect', 'risk',
    'nutrition', 'metabolism', 'transport', 'development', 'regulation', 'production',
})
_CANONICAL_MALFORMED_RE = re.compile(
    r'^(?:to\s+[a-z]+|exposed\s+to|when\s+exposed|increasing\s+the\b|decreasing\s+the\b)\b', re.I,
)
_CANONICAL_UNIT_RE = re.compile(
    r'(?:\b\d+(?:\.\d+)?\s*(?:mg|ug|µg|kg|g|ml|l|mmol|µmol|ppm)\b|[≥≤%])', re.I,
)
_CANONICAL_REL_VERB_RE = re.compile(
    r'\b(?:caused|causes|increased|decreased|affected|correlated|correlates|promoted|reduced|enhanced|stimulated)\b', re.I,
)
_PRONOUN_ANCHOR_RE = re.compile(r'^(?:it|its|they|their|them|this|that|these|those)$', re.I)


def _expand_for_canonical(text, aliases, endpoint_casefold):
    raw = str(text or '')
    expanded = raw
    for alias, row in aliases:
        expanded = alias_pattern(alias).sub(lambda _: row['expansion'], expanded)
    if 'phosphorus' in endpoint_casefold:
        expanded = re.sub(r'(?<!\w)P(?!\w)', 'phosphorus', expanded)
    tokens = set(re.findall(r'[a-z0-9]+', raw.casefold()))
    tokens.update(re.findall(r'[a-z0-9]+', expanded.casefold()))
    return tokens


def _canonical_token_matches(ep_tok, pool_tokens):
    if ep_tok in pool_tokens:
        return True
    ep_base = ep_tok.rstrip('s')
    for tok in pool_tokens:
        if tok.rstrip('s') == ep_base:
            return True
        if len(ep_tok) >= 4 and len(tok) >= 4 and (tok.startswith(ep_tok[:4]) or ep_tok.startswith(tok[:4])):
            return True
    return False


def _canonical_endpoint_anchor(endpoint, proposed_anchor, sentences, glossary=()):
    ep_clean = str(endpoint or '').strip()
    prop_clean = str(proposed_anchor or '').strip()
    if not ep_clean or not prop_clean or _PRONOUN_ANCHOR_RE.fullmatch(prop_clean):
        return None
    ep_words = re.findall(r'[\w-]+', ep_clean)
    if not 1 <= len(ep_words) <= 9:
        return None
    if (_CANONICAL_MALFORMED_RE.match(ep_clean)
            or _CANONICAL_UNIT_RE.search(ep_clean)
            or _CANONICAL_REL_VERB_RE.search(ep_clean)):
        return None

    matched_spans = []
    for source in sentences:
        if span := source_surface_mention(source, prop_clean):
            matched_spans.append((span, source))
    if not matched_spans:
        return None

    documents = {entry.get('document') for entry in glossary if entry.get('document')}
    aliases = []
    if len(documents) == 1:
        resolved, _ = resolved_aliases(glossary, next(iter(documents)))
        aliases = sorted(resolved.items(), key=lambda pair: -len(pair[0]))

    ep_expanded = ep_clean
    for alias, row in aliases:
        ep_expanded = alias_pattern(alias).sub(lambda _: row['expansion'], ep_expanded)
    ep_casefold = ep_expanded.casefold()
    ep_tokens = [t for t in re.findall(r'[a-z0-9]+', ep_casefold) if t not in _CANONICAL_STOP]
    if not ep_tokens:
        return None
    core_tokens = [t for t in ep_tokens if t not in _CANONICAL_HEAD_NOUNS]

    verified = set()
    for span, source in matched_spans:
        anchor_tokens = _expand_for_canonical(span, aliases, ep_casefold)
        source_tokens = _expand_for_canonical(source, aliases, ep_casefold)
        if not any(_canonical_token_matches(t, anchor_tokens) for t in ep_tokens):
            continue
        if core_tokens and not all(_canonical_token_matches(t, source_tokens) for t in core_tokens):
            continue
        verified.add(span)
    if len(verified) == 1:
        return next(iter(verified)), 'canonical_anchored', None
    return None


def endpoint_anchor(endpoint, proposed_anchor, sentences, glossary=(), *, allow_canonical=False):
    """Validate the actual endpoint, not merely a model-provided quote."""
    for source in sentences:
        if span := source_surface_mention(source, endpoint):
            return span, 'literal_endpoint', None
    documents = {entry.get('document') for entry in glossary if entry.get('document')}
    if len(documents) == 1:
        matches = [match for source in sentences
                   if (match := abbreviation_source_anchor(endpoint, source, glossary, next(iter(documents))))]
        if len({match[0] for match in matches}) == 1:
            return matches[0][0], 'document_defined_abbreviation', matches[0][1]
    matches = set()
    for source in sentences:
        spans = coordination_spans(source, endpoint)
        # A separately supplied quote must contain the supporting coordination.
        # When the optional anchor was omitted, it defaults to the endpoint.
        if proposed_anchor and proposed_anchor != endpoint:
            anchor = source_surface_mention(source, proposed_anchor)
            spans = {span for span in spans if anchor and source_surface_mention(anchor, span)}
        matches.update(spans)
    if len(matches) == 1:
        return next(iter(matches)), 'bounded_coordination', None
    if allow_canonical:
        return _canonical_endpoint_anchor(endpoint, proposed_anchor, sentences, glossary)
    return None
