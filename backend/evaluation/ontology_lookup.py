"""Disk-backed candidate lookup for the single-stage experiment."""

import gzip
import html
import json
import re
import sqlite3
from collections import ChainMap
from contextlib import closing
from hashlib import file_digest
from itertools import combinations
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4
from xml.etree import ElementTree as ET

from backend.domain.abbreviations import replace_alias, resolved_aliases
from backend.shared.cancellation import check_cancelled
from backend.shared.storage import stable_hash


POLICY = 'single-stage-lexical-lookup-v1'
RDF = '{http://www.w3.org/1999/02/22-rdf-syntax-ns#}'
OWL = '{http://www.w3.org/2002/07/owl#}'
RDFS = '{http://www.w3.org/2000/01/rdf-schema#}'
SKOS = '{http://www.w3.org/2004/02/skos/core#}'
OIO = '{http://www.geneontology.org/formats/oboInOwl#}'
OBO = '{http://purl.obolibrary.org/obo/}'
LANG = '{http://www.w3.org/XML/1998/namespace}lang'
TYPES = {'http://www.w3.org/2002/07/owl#Class', 'http://www.w3.org/2000/01/rdf-schema#Class'}
WORDS = re.compile(r'[^\W_]+')
STOP = frozenset('a an and are as at be been both but by could did do for from had has have how in into is it its may '
                 'of on or that the their these this those to under was were when where which while will with would'.split())
SYNONYMS = (OIO + 'hasExactSynonym', SKOS + 'altLabel', OIO + 'hasRelatedSynonym',
            OIO + 'hasBroadSynonym', OIO + 'hasNarrowSynonym')
MAX_CLASSES = 56
MAX_PARENTS = 14
ENDPOINT_POLICY = 'source-endpoint-context-lookup-v1'
ENDPOINT_LIMIT = 10

_CONDITION_HEADS = {'availability', 'condition', 'conditions', 'deficiency', 'exposure',
                    'scarcity', 'shortage', 'supply', 'treatment'}
_TRAIT_HEADS = {'area', 'areas', 'concentration', 'concentrations', 'content', 'contents', 'efficiency',
                'efficiencies', 'length', 'lengths', 'mass', 'masses', 'quality', 'ratio', 'ratios',
                'trait', 'traits', 'weight', 'weights', 'width', 'widths'}
_COLLECTIVE_STRUCTURE_HEADS = {'network', 'networks', 'system', 'systems', 'architecture', 'architectures',
                               'canopy', 'canopies', 'foliage', 'crown', 'crowns'}
_LIMITATION_TOKENS = {'low', 'lp', 'limited', 'limiting', 'deficiency', 'deficient', 'scarcity',
                      'shortage', 'depletion', 'starvation', 'absence'}
_NUTRIENT_TOKENS = {'phosphorus', 'phosphate', 'nitrogen', 'potassium', 'magnesium', 'nutrient', 'macronutrient'}


def words(text):
    return tuple(WORDS.findall(text.casefold()))


def _source_aliases(definitions):
    documents = {row.get('document') for row in definitions if row.get('document')}
    if len(documents) == 1:
        resolved, _ = resolved_aliases(definitions, next(iter(documents)))
        return sorted(resolved.items(), key=lambda pair: -len(pair[0]))
    return []


def _expand(text, aliases):
    hints = {}
    for alias, row in aliases:
        text, count = replace_alias(text, alias, row['expansion'])
        if count:
            hints[alias] = row['expansion']
    return text, hints


def search_keys(text):
    tokens = words(text)
    if not tokens or all(token in STOP or token.isdecimal() for token in tokens):
        return
    yield ' '.join(tokens)
    # Ontology labels often end in a category word absent from the source phrase.
    forms = [tokens, tokens[:-1]] if len(tokens) > 1 and tokens[-1] == 'trait' else [tokens]
    for form in forms:
        if form != tokens:
            yield ' '.join(form)
        content = [token for token in form if token not in STOP]
        if len(content) == 2:
            yield '~' + ' '.join(sorted(content))


def class_records(path, release):
    opener = gzip.open if path.suffix == '.gz' else open
    with opener(path, 'rb') as stream:
        parser = ET.iterparse(stream, events=('start', 'end'))
        _, root = next(parser)
        depth = 0
        for event, element in parser:
            if event == 'start':
                depth += 1
                continue
            if depth == 1:
                for node in element.iter():
                    iri = node.get(RDF + 'about', '')
                    if not iri.startswith(('http://', 'https://')):
                        continue
                    typed = node.tag in {OWL + 'Class', RDFS + 'Class'} or (
                        node.tag == RDF + 'Description'
                        and any(child.get(RDF + 'resource') in TYPES for child in node.findall(RDF + 'type')))
                    if not typed:
                        continue
                    label = next((child.text for child in node.findall(RDFS + 'label')
                                  if child.text and child.get(LANG) in (None, 'en')), '')
                    definition = node.findtext(OBO + 'IAO_0000115') or node.findtext(SKOS + 'definition') or ''
                    parents = sorted({parent.get(RDF + 'resource') or parent[0].get(RDF + 'about')
                                      for parent in node.findall(RDFS + 'subClassOf')
                                      if parent.get(RDF + 'resource') or (len(parent) and parent[0].get(RDF + 'about'))})
                    synonyms = [(child.text, rank) for rank, name in enumerate(SYNONYMS, 1)
                                for child in node.findall(name) if child.text and child.get(LANG) in (None, 'en')]
                    info = {'iri': iri, 'label': label, 'definition': definition, 'parents': parents, 'kind': 'class',
                            'exact_synonyms': sorted({text for text, rank in synonyms if rank == 1}), 'source': release}
                    deprecated = int(node.findtext(OWL + 'deprecated', '').casefold() in {'true', '1'})
                    yield info, deprecated, [(label, 0), *synonyms]
                root.remove(element)
            depth -= 1


def open_lookup(directory):
    directory = Path(directory)
    manifest = directory / 'inventory.json'
    if not manifest.is_file():
        raise ValueError('The full ontology releases are missing from analysis/ontology_full_releases.')
    releases = list(json.loads(manifest.read_text(encoding='utf-8')).values())
    imports = directory / 'imports.json'
    if imports.is_file():
        releases.extend(json.loads(imports.read_text(encoding='utf-8'))['modules'].values())
    signature = stable_hash(json.dumps([POLICY, [(row['file'], row['sha256']) for row in releases]]), 24)
    database = directory / f'lookup-{signature}.sqlite3'
    if not database.is_file():
        temporary = directory / f'.lookup-{uuid4().hex}.sqlite3'
        try:
            build_index(directory, releases, temporary)
            try:
                temporary.rename(database)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)
    return sqlite3.connect(database.resolve().as_uri() + '?mode=ro&immutable=1', uri=True), signature


def build_index(directory, releases, destination):
    with closing(sqlite3.connect(destination)) as db:
        db.executescript("""
            PRAGMA journal_mode=OFF;
            PRAGMA synchronous=OFF;
            CREATE TABLE terms(iri TEXT PRIMARY KEY, priority INTEGER, obsolete INTEGER, info TEXT) WITHOUT ROWID;
            CREATE TABLE names(key TEXT, weight INTEGER, iri TEXT);
        """)
        with db:
            for release in releases:
                check_cancelled()
                path = directory / release['file']
                with path.open('rb') as f:
                    if file_digest(f, 'sha256').hexdigest() != release['sha256']:
                        raise ValueError(f'Full ontology release hash changed: {release["file"]}.')
                source = release['file']
                prefix = source.split('.')[0].casefold().replace('bfo_2020', 'bfo')
                terms, names = [], []
                for info, deprecated, synonyms in class_records(path, source):
                    iri = info['iri']
                    owner = int(iri.rsplit('/', 1)[-1].casefold().startswith((prefix + '_', prefix + ':')))
                    priority = 8 * owner + 4 * bool(info['label']) + 2 * bool(info['definition']) + bool(info['parents'])
                    terms.append((iri, priority, deprecated, json.dumps(info, ensure_ascii=False, separators=(',', ':'))))
                    names.extend((key, weight, iri) for synonym, weight in synonyms for key in set(search_keys(synonym)))
                    if len(terms) >= 10_000:
                        check_cancelled()
                        insert_rows(db, terms, names)
                        terms.clear()
                        names.clear()
                insert_rows(db, terms, names)
            db.execute('CREATE INDEX name_hits ON names(key, weight, iri)')


def insert_rows(db, terms, names):
    db.executemany("""INSERT INTO terms VALUES (?, ?, ?, ?) ON CONFLICT(iri) DO UPDATE SET
        priority=max(terms.priority, excluded.priority), obsolete=max(terms.obsolete, excluded.obsolete),
        info=CASE WHEN excluded.priority > terms.priority THEN excluded.info ELSE terms.info END""", terms)
    db.executemany('INSERT INTO names VALUES (?, ?, ?)', names)


def lookup_phrases(visible, owned, definitions):
    aliases = _source_aliases(definitions)
    phrases, hints = {}, {}
    for number, text in visible.items():
        expanded, used = _expand(text, aliases)
        hints.update(used)
        for version in dict.fromkeys((text, expanded)):
            for line in re.split(r'[,;!?]|\.\s+', version):
                surface = WORDS.findall(line)
                tokens = tuple(token.casefold() for token in surface)
                for start, first in enumerate(tokens):
                    if first in STOP or first.isdecimal() or len(first) < 2:
                        continue
                    for end in range(start + 1, min(start + 7, len(tokens) + 1)):
                        last = tokens[end - 1]
                        if last in STOP or last.isdecimal():
                            continue
                        if end == start + 1 and len(first) <= 3 and surface[start].isupper():
                            continue
                        phrase = ' '.join(tokens[start:end])
                        score = (end - start, number in owned, True)
                        phrases[phrase] = max(phrases.get(phrase, score), score)
                    for second in tokens[start + 1:start + 8]:
                        if second in STOP or len(second) < 3 or second.isdecimal():
                            continue
                        # A bounded token pair finds inversions such as "quality of … starch".
                        for variant in (second, second[:-1]) if second.endswith('s') and len(second) > 4 else (second,):
                            phrase = '~' + ' '.join(sorted((first, variant)))
                            score = (2, number in owned, False)
                            phrases[phrase] = max(phrases.get(phrase, score), score)
    return phrases, hints


def passage_candidates(db, catalog, visible, owned, definitions=()):
    phrases, hints = lookup_phrases(visible, owned, definitions)
    matches = {}
    for phrase, score in phrases.items():
        limit = 6 if score[0] > 1 else 3
        for weight, iri, encoded in db.execute("""
                SELECT DISTINCT n.weight, n.iri, t.info FROM names n JOIN terms t USING (iri)
                WHERE n.key=? AND NOT t.obsolete ORDER BY n.weight, n.iri LIMIT ?
                """, (phrase, limit)):
            rank = (*score, -weight, 0)
            if iri not in matches or rank > matches[iri][0]:
                matches[iri] = rank, phrase, encoded
    ranked = sorted(matches, key=lambda iri: (matches[iri][0], iri), reverse=True)[:MAX_CLASSES]
    terms = {}

    def facts(iri):
        if iri not in terms:
            hit = db.execute('SELECT obsolete, info FROM terms WHERE iri=?', (iri,)).fetchone()
            if hit and not hit[0]:
                row = json.loads(hit[1])
                original = catalog.terms.get(iri, {})
                row['parents'] = sorted(set(row['parents']) | set(original.get('parents', ())))
                row['label'] = row['label'] or original.get('label') or iri.rsplit('/', 1)[-1]
                terms[iri] = row
        return terms.get(iri) or catalog.terms.get(iri, {})

    selected = list(ranked)
    lineage = set()
    frontier = list(ranked)
    while frontier:
        iri = frontier.pop(0)
        if iri in lineage:
            continue
        lineage.add(iri)
        row = facts(iri)
        for parent in row.get('parents', ()):
            if parent not in lineage:
                frontier.append(parent)
            parent_row = facts(parent)
            if (parent_row.get('definition') and not parent_row.get('deprecated')
                    and parent not in selected and len(selected) < len(ranked) + MAX_PARENTS):
                selected.append(parent)
    projected = SimpleNamespace(terms=ChainMap(terms, catalog.terms), graph=catalog.graph)
    return projected, {iri: matches[iri][1] if iri in matches else 'upstream parent' for iri in selected}, hints


def endpoint_candidates(db, catalog, endpoint, evidence, definitions=(), limit=ENDPOINT_LIMIT):
    """Rank suggestions for one anchored mention. Search rank never authorizes a mapping."""
    aliases = _source_aliases(definitions)
    expanded, hints = _expand(endpoint, aliases)
    tokens = tuple(token for token in words(expanded) if token not in STOP and not token.isdecimal())
    if not tokens or (len(tokens) == 1 and len(tokens[0]) <= 3 and endpoint.isupper() and not hints):
        return []
    singular = {token[:-1] for token in tokens if len(token) > 4 and token.endswith('s')
                and not token.endswith(('ss', 'us'))}
    query = set(tokens) | singular
    context = set(words(' '.join(_expand(line, aliases)[0] for line in evidence))) - STOP
    # Use the phrase's head for ranking. The cited sentence then helps distinguish senses
    # of the same words, without supplying new mention text or asserting a class.
    head_phrase = re.split(r'\b(?:in|with|under|from)\b', expanded, maxsplit=1, flags=re.I)[0]
    heads = set(words(head_phrase))
    condition = (
        bool(heads & (_CONDITION_HEADS | {'level', 'levels', 'inoculation', 'coinoculation'}))
        or (bool(heads & _LIMITATION_TOKENS) and bool(query & _NUTRIENT_TOKENS))
    )
    trait = not condition and bool(heads & _TRAIT_HEADS)
    collective_structure = not condition and not trait and bool(heads & _COLLECTIVE_STRUCTURE_HEADS)
    phrases, _ = lookup_phrases({1: expanded}, {1}, ())
    for key in search_keys(expanded):
        phrases.setdefault(key, (len(tokens), True, not key.startswith('~')))
    # Prepositional and pre-modified organ phrases should surface pre-composed "<organ> system ..." TO labels.
    organ_singular_map = (
        (('shoot', 'shoots'), 'shoot'),
        (('root', 'roots'), 'root'),
        (('leaf', 'leaves', 'foliar'), 'leaf'),
        (('stem', 'stems'), 'stem'),
        (('tuber', 'tubers'), 'tuber'),
    )
    m_prep_org = re.match(r'^(.+?)\s+(?:in|of|within|from)\s+(?:the\s+)?(.+)$', expanded, re.I)
    if trait and m_prep_org:
        prep_words = [w for w in words(m_prep_org.group(1)) if w not in STOP]
        if prep_words and prep_words[-1] == 'contents':
            prep_words[-1] = 'content'
        prep_head = ' '.join(prep_words)
        org_tokens = set(words(m_prep_org.group(2)))
        if prep_head:
            for triggers, org_sing in organ_singular_map:
                if org_tokens & set(triggers):
                    phrases.setdefault(f'{org_sing} {prep_head}', (min(len(tokens), 4), True, True))
                    phrases.setdefault(f'{org_sing} system {prep_head}', (min(len(tokens), 4), True, True))
    m_lead_org = re.match(r'^(shoots?|roots?|tubers?|stems?|leaves|leaf)\s+(.+)$', expanded, re.I)
    if trait and m_lead_org:
        lead_org = m_lead_org.group(1).casefold().rstrip('s')
        if lead_org == 'leave':
            lead_org = 'leaf'
        rest_words = [w for w in words(m_lead_org.group(2)) if w not in STOP]
        if rest_words and rest_words[-1] == 'contents':
            rest_words[-1] = 'content'
        if rest_words:
            rest_str = ' '.join(rest_words)
            phrases.setdefault(f'{lead_org} {rest_str}', (min(len(tokens), 4), True, True))
            phrases.setdefault(f'{lead_org} system {rest_str}', (min(len(tokens), 4), True, True))
    for token in singular:
        phrases.setdefault(token, (1, True, True))
    accumulated = {}
    for phrase, match in phrases.items():
        for weight, iri, info in db.execute("""
                SELECT DISTINCT n.weight, n.iri, t.info FROM names n JOIN terms t USING (iri)
                WHERE n.key=? AND NOT t.obsolete ORDER BY n.weight, n.iri LIMIT 16
                """, (phrase,)):
            strength = (min(match[0], len(query)), int(match[2]), -weight)
            if iri not in accumulated or strength > accumulated[iri][0]:
                accumulated[iri] = strength, phrase, json.loads(info)

    # Collective organ structures (e.g., "root network") should surface "<organ> system" classes.
    if collective_structure:
        for tok in tokens:
            if tok in _COLLECTIVE_STRUCTURE_HEADS or len(tok) < 3:
                continue
            for base_tok in ({tok, tok[:-1]} if len(tok) > 4 and tok.endswith('s') else {tok}):
                sys_phrase = f'{base_tok} system'
                for weight, iri, encoded in db.execute("""
                        SELECT DISTINCT n.weight, n.iri, t.info FROM names n JOIN terms t USING (iri)
                        WHERE n.key=? AND NOT t.obsolete ORDER BY n.weight, n.iri LIMIT 8
                        """, (sys_phrase,)):
                    accumulated.setdefault(iri, ((2, 0, -weight), sys_phrase, json.loads(encoded)))

    # Complete adjacent prefixes recover variants such as "root dry mass" / "root dry weight".
    for left, right in zip(tokens, tokens[1:]):
        prefix = f'{left} {right}'
        if left == right or len(left) < 3 or len(right) < 2:
            continue
        for iri, info in db.execute("""
                SELECT n.iri, t.info FROM names n JOIN terms t USING (iri)
                WHERE n.key>=? AND n.key<? AND NOT t.obsolete
                ORDER BY n.key, n.weight, n.iri LIMIT 32
                """, (prefix + ' ', prefix + '!')):
            accumulated.setdefault(iri, ((2, 0, -1), prefix, json.loads(info)))

    # Supplement exact/prefix hits with SQLite FTS5 & atomic PATO quality-head lookups
    # across all searchable classes (dense semantics are handled separately via BGE + RRF).
    has_fts = db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='terms_fts'").fetchone() is not None
    extra_rows = []
    head_qualities = set()
    head_quality_words = set()
    pato_head_iris = set()
    if has_fts:
        def _fts_terms(items):
            return [f'"{tok}"' for tok in items if re.fullmatch(r'[a-z0-9]+', tok) and len(tok) >= 2]

        def _query_fts(sources, fts_expr, max_rows=24):
            if not fts_expr:
                return []
            placeholders = ','.join('?' for _ in sources)
            return db.execute(
                f'SELECT t.iri, t.info FROM terms_fts f JOIN terms t USING (iri) '
                f'WHERE f.source IN ({placeholders}) AND f.kind=\'class\' AND NOT t.obsolete '
                f'AND terms_fts MATCH ? ORDER BY bm25(terms_fts, 10.0, 6.0, 1.0) LIMIT ?',
                (*sources, fts_expr, max_rows),
            ).fetchall()

        if condition:
            fts_q = ' OR '.join(_fts_terms(sorted(query)))
            extra_rows.extend(_query_fts(('peco.owl',), fts_q, 16))
            extra_rows.extend(_query_fts(('envo.owl',), fts_q, 12))

        if trait or collective_structure:
            trait_tokens = [t for t in tokens if len(t) >= 2]
            fts_and = ' AND '.join(_fts_terms(dict.fromkeys(trait_tokens))) if 1 <= len(trait_tokens) <= 4 else ''
            fts_or = ' OR '.join(_fts_terms(sorted(set(trait_tokens) | singular)))
            if fts_and:
                extra_rows.extend(_query_fts(('to.owl', 'pato.owl'), fts_and, 16))
            extra_rows.extend(_query_fts(('to.owl', 'pato.owl'), fts_or, 16))
            head_qualities = {tok.removesuffix('s') if tok.endswith('s') and tok not in {'mass'} else tok
                              for tok in (heads & _TRAIT_HEADS) | (set(tokens) & _TRAIT_HEADS)}
            if 'masses' in head_qualities:
                head_qualities.add('mass')
            pato_lookups = set(head_qualities)
            head_quality_words = set(head_qualities)
            for prev_tok, cur_tok in zip(tokens, tokens[1:]):
                cur_norm = cur_tok.removesuffix('s') if cur_tok.endswith('s') and cur_tok not in {'mass'} else cur_tok
                if cur_norm in _TRAIT_HEADS and prev_tok not in STOP and len(prev_tok) >= 3:
                    pato_lookups.add(f'{prev_tok} {cur_norm}')
                    head_quality_words.update((prev_tok, cur_norm))
            for hq in sorted(pato_lookups):
                for iri, encoded in db.execute(
                        "SELECT DISTINCT t.iri, t.info FROM names n JOIN terms t USING (iri) "
                        "WHERE n.key IN (?, ?) AND t.iri LIKE '%/PATO_%' AND NOT t.obsolete LIMIT 6",
                        (hq, hq + ' of')):
                    extra_rows.append((iri, encoded))
                    pato_head_iris.add(iri)

        if collective_structure:
            organs = [t for t in tokens if t not in _COLLECTIVE_STRUCTURE_HEADS and len(t) >= 3]
            if organs:
                po_expr = f"({' OR '.join(_fts_terms(organs))}) AND (\"collective\" OR \"system\")"
                extra_rows.extend(_query_fts(('po.owl',), po_expr, 12))
    else:
        extra_rows.extend(db.execute('SELECT iri, info FROM terms WHERE NOT obsolete LIMIT 500').fetchall())

    def _put(iri, strength, phrase, row):
        if iri not in accumulated or strength > accumulated[iri][0]:
            accumulated[iri] = strength, phrase, row

    for iri, encoded in extra_rows:
        row = json.loads(encoded)
        label_words = set(words(row.get('label', '')))
        meaning = set(words(row.get('definition', '')))
        prefix = iri.rsplit('/', 1)[-1].partition('_')[0]
        compatible_kind = ((condition and prefix in {'PECO', 'ECTO', 'ENVO'})
                           or (trait and prefix in {'TO', 'PATO'}))
        peco_limited_p = (
            condition and prefix == 'PECO'
            and bool(query & _NUTRIENT_TOKENS)
            and bool(query & _LIMITATION_TOKENS)
            and 'phosphate' in (label_words | meaning)
            and bool((label_words | meaning) & {'limited', 'limiting', 'absence'})
        )
        po_collective = (
            collective_structure and prefix == 'PO'
            and ('collective' in meaning or 'system' in label_words
                 or 'http://purl.obolibrary.org/obo/PO_0025007' in row.get('parents', ()))
            and bool((query - _COLLECTIVE_STRUCTURE_HEADS) & (label_words | meaning))
        )
        peco_phosphate = (
            iri == 'http://purl.obolibrary.org/obo/PECO_0001006'
            and condition and bool(query & {'phosphate', 'phosphorus'})
        )
        if peco_limited_p:
            _put(iri, (2, 0, 0), 'limited nutrient exposure candidate', row)
        elif peco_phosphate:
            _put(iri, (1, 0, 0), 'phosphate exposure candidate', row)
        elif iri in pato_head_iris and (label_words - STOP) <= head_quality_words:
            _put(iri, (3, 1, 0), 'quality head candidate', row)
        elif iri in pato_head_iris:
            _put(iri, (1, 1, 0), 'quality head candidate', row)
        elif po_collective:
            _put(iri, (2, 0, 0), 'collective structure candidate', row)
        elif len(query & label_words) >= 2 or (compatible_kind and query & (label_words | meaning)):
            _put(iri, (0, 0, 0), 'definition or label', row)

    def rank(item):
        iri, (strength, _, row) = item
        prefix = iri.rsplit('/', 1)[-1].partition('_')[0]
        label = set(words(row['label'])) - STOP
        definition = set(words(row.get('definition', ''))) - STOP
        is_collective_po = (
            prefix == 'PO'
            and ('collective' in definition or 'system' in label
                 or 'http://purl.obolibrary.org/obo/PO_0025007' in row.get('parents', ()))
        )
        compatible_kind = ((condition and prefix in {'PECO', 'ECTO', 'ENVO'})
                           or (trait and prefix in {'TO', 'PATO'})
                           or (collective_structure and is_collective_po))
        kind_conflict = condition and prefix in {'CHEBI', 'NCBITaxon'}
        coverage = len(query & label) / len(tokens)
        wording = 3 * strength[0] + strength[1] + .25 * strength[2] + 4 * coverage
        meaning = min(2, len(query & definition)) * .5 + min(5, len(context & definition)) * .18
        return wording + meaning + 3 * compatible_kind - 8 * kind_conflict, coverage, iri

    candidates = sorted(accumulated.items(), key=rank, reverse=True)[:limit]
    return [{
        **{key: row.get(key, '') for key in ('iri', 'label', 'definition', 'parents')},
        'match': match,
        'class_source': row.get('source', ''),
    } for _, (_, match, row) in candidates]


_OWL_NARROW_FILE_CACHE: dict[str, dict[str, tuple[str, ...]]] = {}
_OWL_NARROW_TERM_CACHE: dict[tuple[str, str], tuple[str, ...]] = {}


def _owl_path(lookup_dir, class_source):
    if not lookup_dir or not class_source:
        return None
    base = Path(lookup_dir)
    for candidate in (base / class_source, base / 'imports' / class_source):
        if candidate.is_file():
            return candidate
    return None


def _read_narrow_synonyms_stream(path: Path) -> dict[str, tuple[str, ...]]:
    """Index only IRIs that carry oboInOwl:hasNarrowSynonym from one OWL file."""
    narrow_tag = OIO + 'hasNarrowSynonym'
    opener = gzip.open if path.suffix == '.gz' else open
    found: dict[str, list[str]] = {}
    with opener(path, 'rb') as stream:
        parser = ET.iterparse(stream, events=('start', 'end'))
        _, root = next(parser)
        depth = 0
        for event, element in parser:
            if event == 'start':
                depth += 1
                continue
            if depth == 1:
                for node in element.iter():
                    iri = node.get(RDF + 'about', '')
                    if not iri:
                        continue
                    syns = [
                        child.text.strip()
                        for child in node.findall(narrow_tag)
                        if child.text and child.text.strip() and child.get(LANG) in (None, 'en')
                    ]
                    if syns:
                        found.setdefault(iri, []).extend(syns)
                root.remove(element)
            depth -= 1
    return {iri: tuple(dict.fromkeys(vals)) for iri, vals in found.items()}


def _owl_narrow_synonyms_for_iri(path: Path, iri: str) -> tuple[str, ...]:
    path_key = str(path.resolve())
    if path_key in _OWL_NARROW_FILE_CACHE:
        return _OWL_NARROW_FILE_CACHE[path_key].get(iri, ())
    term_key = (path_key, iri)
    if term_key in _OWL_NARROW_TERM_CACHE:
        return _OWL_NARROW_TERM_CACHE[term_key]

    if path.suffix != '.gz' and path.stat().st_size <= 15_000_000:
        mapping = _read_narrow_synonyms_stream(path)
        _OWL_NARROW_FILE_CACHE[path_key] = mapping
        return mapping.get(iri, ())

    # Large compressed/uncompressed OWL fallback: scan bytes for exact rdf:about block first.
    target_bytes = f'"{iri}"'.encode('utf-8')
    opener = gzip.open if path.suffix == '.gz' else open
    narrow_tag = OIO + 'hasNarrowSynonym'
    syns: list[str] = []
    found_block = False
    with opener(path, 'rb') as stream:
        capturing = False
        buf: list[bytes] = []
        close_tag = b''
        for raw_line in stream:
            if not capturing:
                if target_bytes in raw_line and (b'<owl:Class' in raw_line or b'<rdf:Description' in raw_line or b'<rdfs:Class' in raw_line):
                    close_tag = (
                        b'</owl:Class>' if b'<owl:Class' in raw_line
                        else b'</rdf:Description>' if b'<rdf:Description' in raw_line
                        else b'</rdfs:Class>'
                    )
                    if b'/>' in raw_line and b'hasNarrowSynonym' not in raw_line:
                        found_block = True
                        break
                    capturing = True
                    buf = [raw_line]
                    if close_tag in raw_line:
                        found_block = True
                        break
            else:
                buf.append(raw_line)
                if close_tag in raw_line:
                    found_block = True
                    break
        if found_block and buf:
            text_block = b''.join(buf).decode('utf-8', errors='replace')
            for m in re.finditer(r'<[^>]*hasNarrowSynonym\b[^>]*>(.*?)</[^>]*hasNarrowSynonym>', text_block, re.S):
                val = html.unescape(re.sub(r'\s+', ' ', m.group(1))).strip()
                if val:
                    syns.append(val)
    if not found_block:
        mapping = _read_narrow_synonyms_stream(path)
        _OWL_NARROW_FILE_CACHE[path_key] = mapping
        return mapping.get(iri, ())
    res = tuple(dict.fromkeys(syns))
    _OWL_NARROW_TERM_CACHE[term_key] = res
    return res


def verify_narrow_synonym_in_owl(lookup_dir, iri, class_source, wanted_keys):
    """Verify oboInOwl:hasNarrowSynonym directly in the OWL file for a shortlisted IRI."""
    path = _owl_path(lookup_dir, class_source)
    if path is None:
        return None
    wanted = {' '.join(words(str(k))) for k in wanted_keys if str(k).strip()}
    if not wanted:
        return None
    for syn_text in _owl_narrow_synonyms_for_iri(path, iri):
        norm = ' '.join(words(syn_text))
        stripped = norm[:-7].strip() if norm.endswith(' narrow') else norm
        for candidate_key in (norm, stripped):
            if candidate_key and candidate_key in wanted:
                return {
                    'verified_narrow_synonym': syn_text,
                    'synonym_source': class_source,
                    'class_source': class_source,
                    'normalized_index_key': candidate_key,
                }
    return None
