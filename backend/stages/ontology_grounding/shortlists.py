"""Nominate established terms. Only source evidence and definitions justify a binding."""
from collections import defaultdict
from contextlib import closing
from functools import lru_cache
import json
from pathlib import Path
import re
import sqlite3

from backend.domain.abbreviations import alias_pattern, resolved_aliases
from backend.domain.treatment_labels import LABEL_CUE, CODE
from backend.shared.storage import stable_hash
from backend.ontology.property_types import effective_property_restrictions


_STOP = set('a an the at of for from to in on by under as and or is was has have had were with that its than more significantly'.split())


def _words(text):
    words = re.findall(r'[a-z0-9]+', text.casefold())
    return {word.removesuffix('s') if len(word) > 3 and not word.endswith('ss') else word
            for word in words if word not in _STOP}


def _score(query, names):
    return max((2 * len(query & words) / (len(query) + len(words))
                for words in names if query & words), default=0)


@lru_cache(maxsize=4)
def _peco_sqlite_names(db_path):
    """Load non-obsolete PECO class labels/synonyms from the searchable SQLite inventory."""
    with closing(sqlite3.connect(Path(db_path).as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute(
            "SELECT iri, info FROM terms "
            "WHERE iri >= 'http://purl.obolibrary.org/obo/PECO_' "
            "AND iri < 'http://purl.obolibrary.org/obo/PECO`' AND NOT obsolete"
        ).fetchall()
    entries = []
    for iri, raw_info in rows:
        term = json.loads(raw_info)
        if term.get('kind', 'class') != 'class' or term.get('deprecated'):
            continue
        names = [_words(s) for s in (term.get('label', ''), *term.get('exact_synonyms', ())) if s]
        if names:
            entries.append((iri, names))
    return tuple(entries)


def _resolve_inventory(inventory):
    if inventory is not None:
        return inventory
    try:
        from backend.ontology.inventory import open_inventory
        return open_inventory()
    except (RuntimeError, FileNotFoundError, ValueError):
        return None


def treatment_candidates(reference, claim, role, glossary, *, inventory=None):
    """Rank study arms from their verified naming sentence, never from the shape of a code."""
    label = claim[role].casefold()
    setups = {}
    for record in claim.get('source_treatment_labels', ()):
        text = record.get('text', '')
        cue = LABEL_CUE.search(text)
        if (cue and record.get('label', '').casefold() == label and claim.get('source_document_id')
                and claim.get('source_pdf_sha256') and record.get('document') == claim['source_document_id']
                and record.get('source_pdf_sha256') == claim['source_pdf_sha256']
                and record.get('text_sha256') == stable_hash(text, 64)
                and label in {m[0].casefold() for m in CODE.finditer(text[cue.end():].split(';', 1)[0])}):
            setups[text] = (text[:cue.start()], record)
    if len(setups) != 1:
        return [], None
    description, record = next(iter(setups.values()))
    aliases, _ = resolved_aliases(glossary, claim['source_document_id'])
    for alias, definition in aliases.items():
        description = alias_pattern(alias).sub(lambda _: definition['expansion'], description)
    query = _words(description)
    inv = _resolve_inventory(inventory)
    if inv is not None:
        peco_entries = _peco_sqlite_names(str(inv.path))
    else:
        peco_entries = [
            (iri, [_words(s) for s in (term.get('label', ''), *term.get('exact_synonyms', ())) if s])
            for iri, term in reference.terms.items()
            if '/PECO_' in iri and term.get('kind', 'class') == 'class' and not term.get('deprecated')
        ]
    ranked = [(score, iri) for iri, names in peco_entries if (score := _score(query, names))]
    return [iri for _, iri in sorted(ranked, key=lambda item: (-item[0], item[1]))[:4]], record


def _stem_words(text):
    return {re.sub(r'(ingly|ing|ed|ion|e)$', '', word) if len(word) > 4 else word
            for word in _words(text)}


@lru_cache(maxsize=8)
def _relation_index(reference):
    index = {}
    for iri in reference.profile['allowed_object_properties']:
        term = reference.terms[iri]
        names = [_stem_words(name) for name in (
            term['label'], *term.get('exact_synonyms', ()), *term.get('obo_unique_labels', ()),
        )]
        metadata = _stem_words(' '.join([term.get('definition') or '', *term.get('related_synonyms', ())]))
        index[iri] = names, metadata, effective_property_restrictions(reference, iri)
    return index


def lexical_properties(reference, predicate, endpoints, limit=10):
    """Rank properties by upstream text and show which endpoint choices have verified types."""
    query = _stem_words(predicate)
    ranked = []
    for iri, (names, metadata, restrictions) in _relation_index(reference).items():
        score = max(_score(query, names), 0.65 * _score(query, [metadata]))
        if not score:
            continue
        matching = {}
        typed = True
        for role, field in [('subject', 'domain'), ('object', 'range')]:
            required = set(restrictions[field])
            if restrictions['unsupported_' + field]:
                typed = False
            if required:
                matching[role] = [alias for alias, ancestry in endpoints[role].items()
                                  if required <= ancestry]
                typed &= bool(matching[role])
        ranked.append((score + 0.2 * bool(typed and matching), iri, matching))
    return [(iri, matching) for _, iri, matching in sorted(ranked, key=lambda x: (-x[0], x[1]))[:limit]]


def cultivar_definitions(claims, reference, *, inventory=None):
    """Accept a named endpoint only within an explicit crop-cultivar enumeration in its own PDF."""
    crop = reference.profile.get('crop')
    species = reference.profile.get('crop_iri')
    if not crop or not species:
        return {}
    inv = _resolve_inventory(inventory)
    if inv is not None:
        if inv.get_term(species) is None and species not in reference.terms:
            return {}
    elif species not in reference.terms:
        return {}
    by_doc = defaultdict(list)
    for claim in claims:
        doc = (claim.get('source_document_id'), claim.get('source_pdf_sha256'))
        if all(doc):
            by_doc[doc].append(claim)
    records = {}
    marker = re.compile(r'\b' + re.escape(crop) + r'\s+(?:cultivars?|varieties|genotypes?)\s*[:,\(\[]?\s*', re.I)
    for rows in by_doc.values():
        names = {r[role].casefold() for r in rows for role in ('subject', 'object')
                 if re.fullmatch(r"[A-Z][\w-]*(?: [A-Z][\w-]*){0,3}", r[role])}
        if not names:
            continue
        alternatives = '|'.join(map(re.escape, sorted(names, key=lambda name: (-len(name), name))))
        named = re.compile(r"""['"‘’]?(""" + alternatives + r""")['"‘’]?(?![\w-])""", re.I)
        found = {}
        for row in rows:
            for field in ('source_evidence_span', 'source_local_context', 'source_reading_context'):
                for passage in (row.get(field) or '').splitlines():
                    for marker_match in marker.finditer(passage):
                        tail = passage[marker_match.end():]
                        while (match := named.match(tail)):
                            found.setdefault(match[1].casefold(), {'text': passage, 'page': row.get('source_page'),
                                'source_claim_id': row['candidate_id'], 'species_iri': species})
                            separator = re.match(r'\s*(?:,\s*(?:and\s+)?|and\s+)', tail[match.end():], re.I)
                            if not separator:
                                break
                            tail = tail[match.end() + separator.end():]
        for row in rows:
            for role in ('subject', 'object'):
                if record := found.get(row[role].casefold()):
                    records[(row['candidate_id'], role)] = record
    return records
