"""Experimental combined local ontology retrieval (namespace scoping + ChEBI formulas + scoped FTS5 BM25)."""
from __future__ import annotations

import gzip
import html
import json
import math
import re
import sqlite3
from collections import Counter, defaultdict
from pathlib import Path

from backend.evaluation.ontology_lookup import endpoint_candidates, words
from backend.shared.artifacts import save_json
from backend.shared.storage import utc_now

_SUBSCRIPT_TRANS = str.maketrans('₀₁₂₃₄₅₆₇₈₉⁺⁻', '0123456789+-')
_SUPERSCRIPT_TRANS = str.maketrans('⁰¹²³⁴⁵⁶⁷⁸⁹', '0123456789')
_ELEMENT_TOKEN_RE = re.compile(r'([A-Z][a-z]?)(\d*)')
_DOSE_CODE_RE = re.compile(r'^p\d+(?:\.\d+)?$', re.I)
_STOPWORDS = frozenset({
    'of', 'the', 'in', 'a', 'an', 'and', 'or', 'to', 'by', 'for', 'with',
    'under', 'at', 'as', 'from', 'across', 'levels', 'conditions',
})

_SCOPED_NON_TAXON_SOURCES = frozenset({
    'to.owl', 'po.owl', 'co_330.owl', 'peco.owl', 'pato.owl', 'cdno.owl',
    'ecto.owl', 'envo.owl', 'uo.owl', 'bfo_2020.owl', 'ro.owl', 'go.owl',
    'foodon.owl', 'hp.owl', 'mondo.owl', 'cmo.owl', 'oba.owl',
    'chebi.owl.gz', 'imports/to/imports/ncbitaxon_import.owl',
    'imports/peco/imports/ncbitaxon_import.owl',
})
_NCBITAXON_ROOTS = frozenset({
    'http://purl.obolibrary.org/obo/NCBITaxon_33090',   # Viridiplantae
    'http://purl.obolibrary.org/obo/NCBITaxon_1386',    # Bacillus <firmicutes>
    'http://purl.obolibrary.org/obo/NCBITaxon_1423',    # Bacillus subtilis
    'http://purl.obolibrary.org/obo/NCBITaxon_286',     # Pseudomonas
    'http://purl.obolibrary.org/obo/NCBITaxon_379',     # Rhizobium
    'http://purl.obolibrary.org/obo/NCBITaxon_214504',  # Glomeromycota
})
_KIND_PREFIX_BOOST = {
    'trait or measurement': {'cmo.owl': 1.35, 'oba.owl': 1.35, 'to.owl': 1.4, 'co_330.owl': 1.35, 'pato.owl': 1.15, 'cdno.owl': 1.15, 'go.owl': 1.05},
    'assay variable': {'cmo.owl': 1.35, 'oba.owl': 1.35, 'co_330.owl': 1.35, 'to.owl': 1.35, 'pato.owl': 1.15},
    'biological process': {'go.owl': 1.35, 'to.owl': 1.1},
    'condition or exposure': {'peco.owl': 1.45, 'ecto.owl': 1.25, 'envo.owl': 1.15, 'to.owl': 1.05, 'go.owl': 1.05},
    'chemical': {'chebi.owl.gz': 1.45, 'cdno.owl': 1.1},
    'organism': {'ncbitaxon.owl.gz': 1.5, 'ecto.owl': 0.85},
    'anatomy or part': {'po.owl': 1.45, 'to.owl': 1.0},
    'food': {'foodon.owl': 1.45, 'chebi.owl.gz': 1.15, 'cdno.owl': 1.15},
    'health outcome': {'hp.owl': 1.4, 'mondo.owl': 1.4, 'go.owl': 1.15, 'pato.owl': 1.05},
}


def _parse_single_formula(segment: str) -> Counter | None:
    """Parse one chemical formula segment with optional stoichiometry and parentheses."""
    s = segment.strip()
    if not s:
        return None
    mult = 1
    m_lead = re.match(r'^(\d+)(?=[A-Z(])', s)
    if m_lead:
        mult = int(m_lead.group(1))
        s = s[m_lead.end():]
    stack: list[Counter] = [Counter()]
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == '(':
            stack.append(Counter())
            i += 1
        elif ch == ')':
            if len(stack) == 1:
                return None
            top = stack.pop()
            i += 1
            m_num = re.match(r'\d+', s[i:])
            fac = int(m_num.group(0)) if m_num else 1
            if m_num:
                i += m_num.end()
            for el, cnt in top.items():
                stack[-1][el] += cnt * fac
        else:
            m_el = _ELEMENT_TOKEN_RE.match(s[i:])
            if not m_el:
                return None
            el, n_str = m_el.group(1), m_el.group(2)
            stack[-1][el] += int(n_str) if n_str else 1
            i += m_el.end()
    if len(stack) != 1 or not stack[0]:
        return None
    return Counter({el: cnt * mult for el, cnt in stack[0].items()})


def parse_chemical_formula(raw: str) -> tuple[str, str | None] | None:
    """Normalize a formula (handling OCR spaces, dots, parentheses, and charge) or return None."""
    txt = re.sub(r'\s+', '', raw or '').replace('\u2212', '-')
    # Decode superscript charge before converting subscript atom counts.
    txt = re.sub(r'([\u2070\u00b9\u00b2\u00b3\u2074-\u2079]*)([\u207a\u207b])$',
                 lambda m: '^' + m[1].translate(_SUPERSCRIPT_TRANS) + m[2].translate(_SUBSCRIPT_TRANS), txt)
    txt = txt.translate(_SUBSCRIPT_TRANS)
    if not txt or _DOSE_CODE_RE.match(txt):
        return None
    charge = None
    match = re.search(r'\^(\d*)([+-])$', txt)
    if not match and re.fullmatch(r'[A-Z][a-z]?\d+[+-]', txt):
        match = re.search(r'(\d+)([+-])$', txt)
    if match:
        magnitude = match[1] or '1'
        charge = ('-' if match[2] == '-' else '') + magnitude
        txt = txt[:match.start()]
    elif txt.endswith(('+', '-')):
        if re.search(r'\d{2,}[+-]$', txt):
            return None  # Flattened count and charge are ambiguous.
        charge = '-1' if txt.endswith('-') else '1'
        txt = txt[:-1]
    if charge is None and len(txt) <= 4 and not any(ch.isdigit() or ch in '.()' for ch in txt):
        return None
    if not re.search(r'[A-Z]', txt):
        return None
    total = Counter()
    for part in txt.split('.'):
        parsed = _parse_single_formula(part)
        if not parsed:
            return None
        total += parsed
    canon = ''.join(f'{el}{total[el]}' for el in sorted(total))
    return canon, charge


def load_or_build_chebi_formulas(full_db: sqlite3.Connection, chebi_owl_path: Path, cache_path: Path) -> dict:
    """Load cached active ChEBI formula map or build from chebi.owl.gz."""
    if cache_path.is_file():
        return json.loads(cache_path.read_text(encoding='utf-8'))

    active_chebi = {
        iri: json.loads(info_s).get('label', '')
        for iri, info_s in full_db.execute("SELECT iri, info FROM terms WHERE obsolete=0 AND iri LIKE '%/CHEBI_%'")
    }
    by_exact: dict[str, list[dict]] = defaultdict(list)
    by_sig: dict[str, list[dict]] = defaultdict(list)
    records = 0

    cur_iri, buf = None, []
    with gzip.open(chebi_owl_path, 'rt', encoding='utf-8', errors='replace') as fh:
        for line in fh:
            if 'rdf:about="http://purl.obolibrary.org/obo/CHEBI_' in line:
                m = re.search(r'<owl:Class rdf:about="(http://purl\.obolibrary\.org/obo/CHEBI_\d+)"', line)
                cur_iri = m.group(1) if (m and m.group(1) in active_chebi) else None
                buf = [line] if cur_iri else []
            elif cur_iri:
                buf.append(line)
                if '</owl:Class>' in line:
                    blk = ''.join(buf)
                    fml_m = re.search(r'<chemrof:generalized_empirical_formula[^>]*>(.*?)</chemrof:generalized_empirical_formula>', blk)
                    if fml_m:
                        raw_fml = html.unescape(fml_m.group(1)).strip()
                        chg_m = re.search(r'<chemrof:charge[^>]*>(.*?)</chemrof:charge>', blk)
                        raw_chg = html.unescape(chg_m.group(1)).strip() if chg_m else '0'
                        total = Counter()
                        ok = True
                        for part in raw_fml.split('.'):
                            p = _parse_single_formula(part)
                            if not p:
                                ok = False
                                break
                            total += p
                        if ok and total:
                            canon = ''.join(f'{el}{total[el]}' for el in sorted(total))
                            rec = {
                                'iri': cur_iri,
                                'label': active_chebi[cur_iri],
                                'raw_formula': raw_fml,
                                'charge': raw_chg,
                                'canonical': canon,
                            }
                            by_sig[canon].append(rec)
                            by_exact[f'{canon}|chg={raw_chg}'].append(rec)
                            records += 1
                    cur_iri, buf = None, []

    payload = {
        'created_at': utc_now(),
        'indexed_classes': records,
        'unique_canonical_signatures': len(by_sig),
        'by_exact': dict(by_exact),
        'by_signature': dict(by_sig),
    }
    save_json(cache_path, payload)
    return payload


def open_or_build_scoped_fts5(full_db: sqlite3.Connection, ref_24, scoped_db_path: Path) -> sqlite3.Connection:
    """Open verified scoped FTS5 SQLite or build it from full_db without modifying full_db."""
    if scoped_db_path.is_file():
        conn = sqlite3.connect(str(scoped_db_path))
        has_fts = conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='terms_fts'").fetchone()
        cnt = conn.execute('SELECT COUNT(*) FROM terms').fetchone()[0] if has_fts else 0
        if has_fts and 0 < cnt < 1_500_000:
            return conn
        conn.close()
        scoped_db_path.unlink()

    scoped_db_path.parent.mkdir(parents=True, exist_ok=True)
    children = defaultdict(list)
    approved_taxa = {iri for iri in ref_24.terms if '/NCBITaxon_' in iri}
    for iri, info_s in full_db.execute("SELECT iri, info FROM terms WHERE obsolete=0 AND iri LIKE '%/NCBITaxon_%'"):
        info = json.loads(info_s)
        for p in info.get('parents') or ():
            children[p].append(iri)

    allowed_taxa = set(_NCBITAXON_ROOTS)
    stack = list(_NCBITAXON_ROOTS)
    while stack:
        cur = stack.pop()
        for ch in children.get(cur, ()):
            if ch not in allowed_taxa:
                allowed_taxa.add(ch)
                stack.append(ch)
    allowed_taxa |= approved_taxa

    sconn = sqlite3.connect(str(scoped_db_path))
    sconn.execute('CREATE TABLE terms(iri TEXT PRIMARY KEY, priority INTEGER, obsolete INTEGER, info TEXT) WITHOUT ROWID')
    sconn.execute('CREATE TABLE names(key TEXT, weight INTEGER, iri TEXT)')
    sconn.execute(
        "CREATE VIRTUAL TABLE terms_fts USING fts5("
        "iri UNINDEXED, source UNINDEXED, kind UNINDEXED, label, exact_synonyms, definition, "
        "tokenize='porter unicode61')"
    )

    kept_iris = set()
    term_rows, fts_rows = [], []
    for iri, prio, obs, info_s in full_db.execute('SELECT iri, priority, obsolete, info FROM terms WHERE obsolete=0'):
        info = json.loads(info_s)
        src = info.get('source', '')
        if not ((src in _SCOPED_NON_TAXON_SOURCES) or (iri in allowed_taxa) or (iri in ref_24.terms)):
            continue
        kept_iris.add(iri)
        term_rows.append((iri, prio, obs, info_s))
        fts_rows.append((
            iri, src, info.get('kind', 'class'),
            info.get('label') or '',
            ' | '.join(info.get('exact_synonyms') or ()),
            info.get('definition') or '',
        ))

    sconn.executemany('INSERT INTO terms VALUES(?, ?, ?, ?)', term_rows)
    sconn.executemany('INSERT INTO terms_fts VALUES(?, ?, ?, ?, ?, ?)', fts_rows)

    name_batch = []
    for key, weight, iri in full_db.execute('SELECT DISTINCT key, weight, iri FROM names'):
        if iri in kept_iris:
            name_batch.append((key, weight, iri))
            if len(name_batch) >= 50000:
                sconn.executemany('INSERT INTO names VALUES(?, ?, ?)', name_batch)
                name_batch.clear()
    if name_batch:
        sconn.executemany('INSERT INTO names VALUES(?, ?, ?)', name_batch)
    sconn.execute('CREATE INDEX name_hits ON names(key, weight, iri)')
    sconn.commit()
    return sconn


def _expand_query_phrase(phrase: str, definitions: list[dict] | tuple[dict, ...]) -> list[str]:
    """Generate raw and abbreviation-expanded forms including inline acronym expansion."""
    variants = [phrase]
    alias_map = {}
    for d in definitions or ():
        abbr = str(d.get('abbreviation') or '').strip()
        exp = str(d.get('expansion') or '').strip()
        if abbr and exp:
            alias_map[abbr.casefold()] = exp
    expanded = phrase
    for abbr_low, exp in sorted(alias_map.items(), key=lambda kv: -len(kv[0])):
        pat = re.compile(rf'(?<![A-Za-z0-9]){re.escape(abbr_low)}(?![A-Za-z0-9])', re.I)
        expanded = pat.sub(exp, expanded)
    if expanded.casefold() != phrase.casefold():
        variants.append(expanded)
    return variants


def formula_lookup_candidates(formula_index: dict, phrase: str, *, limit: int = 10) -> list[dict]:
    """Match an inorganic/organic formula mention against canonicalized active ChEBI formulas."""
    parsed = parse_chemical_formula(phrase)
    if not parsed:
        return []
    canon, chg = parsed
    hits = list(formula_index['by_exact'].get(f'{canon}|chg={chg}', ())) if chg is not None else []
    if chg is None:
        hits = list(formula_index['by_exact'].get(f'{canon}|chg=0', ())) or list(formula_index['by_signature'].get(canon, ()))
    out, seen = [], set()
    for h in hits:
        if h['iri'] in seen:
            continue
        seen.add(h['iri'])
        out.append({
            'iri': h['iri'],
            'label': h['label'],
            'class_source': 'chebi.owl.gz',
            'via': 'chebi_formula',
        })
        if len(out) >= limit:
            break
    return out


def fts5_bm25_candidates(
    sconn: sqlite3.Connection,
    phrase: str,
    kind: str,
    definitions: list[dict] | tuple[dict, ...] = (),
    *,
    limit: int = 10,
) -> list[dict]:
    """Query scoped SQLite FTS5 BM25 with inline abbreviation expansion and token-coverage checks."""
    if _DOSE_CODE_RE.match(phrase.strip()):
        return []
    variants = _expand_query_phrase(phrase, definitions)
    scored: dict[str, tuple[float, dict]] = {}
    for var in variants:
        tokens = [t for t in words(var) if len(t) >= 2 and t not in _STOPWORDS]
        if not tokens:
            continue
        q_and = ' AND '.join(f'"{t}"' for t in tokens)
        queries = [(q_and, 2.5)]
        if len(tokens) >= 2:
            queries.append((' OR '.join(f'"{t}"' for t in tokens), 1.0))

        alt_tokens = [({'concentration': 'content', 'concentrations': 'content', 'deficiency': 'phosphate'}.get(t, t)) for t in tokens]
        if alt_tokens != tokens:
            queries.append((' AND '.join(f'"{t}"' for t in alt_tokens), 2.0))
            if len(alt_tokens) >= 2:
                queries.append((' OR '.join(f'"{t}"' for t in alt_tokens), 0.9))

        min_overlap = max(2, math.ceil(0.6 * len(tokens))) if len(tokens) >= 2 else 1
        token_set = set(tokens) | set(alt_tokens)
        for q_str, mode_boost in queries:
            rows = sconn.execute(
                'SELECT iri, source, label, exact_synonyms, definition, bm25(terms_fts, 10.0, 6.0, 1.0) AS score '
                'FROM terms_fts WHERE terms_fts MATCH ? ORDER BY score LIMIT 40',
                (q_str,),
            ).fetchall()
            for iri, src, lbl, syns, _defn, bm in rows:
                lbl_syns = f'{lbl} {syns}'.lower()
                hit_tokens = sum(1 for t in token_set if t in lbl_syns)
                if mode_boost <= 1.0 and hit_tokens < min_overlap and not any(t in (lbl or '').lower() for t in tokens[:1]):
                    continue
                if len(tokens) >= 2 and hit_tokens == 0:
                    continue
                raw_rel = max(0.1, -float(bm))
                lbl_low = (lbl or '').lower()
                method_pen = 0.7 if (src == 'co_330.owl' and (' - method' in lbl_low or ' - computation' in lbl_low or ' - measurement' in lbl_low)) else 1.0
                exact_bonus = 2.0 if lbl_low.strip() == var.strip().casefold() else 1.0
                src_boost = _KIND_PREFIX_BOOST.get(kind, {}).get(src, 1.0)
                total_score = raw_rel * mode_boost * src_boost * exact_bonus * method_pen
                prev = scored.get(iri)
                if prev is None or total_score > prev[0]:
                    scored[iri] = (total_score, {'iri': iri, 'label': lbl, 'class_source': src, 'via': 'fts5_bm25'})

    ranked = sorted(scored.values(), key=lambda x: (-x[0], x[1]['label']))
    return [row for _, row in ranked[:limit]]


def combined_endpoint_candidates(
    scoped_db: sqlite3.Connection,
    exp_ref,
    formula_index: dict,
    phrase: str,
    kind: str,
    evidence: list[str],
    definitions: list[dict] | tuple[dict, ...] = (),
    *,
    limit: int = 10,
) -> list[dict]:
    """Combined experimental candidate generator (ChEBI formula + interleaved exact/prefix and FTS5 BM25)."""
    fml = formula_lookup_candidates(formula_index, phrase, limit=limit)
    c1 = endpoint_candidates(scoped_db, exp_ref, phrase, evidence, definitions, limit=limit)
    c3 = fts5_bm25_candidates(scoped_db, phrase, kind, definitions, limit=limit)
    interleaved: list[dict] = [*fml]
    for idx in range(max(len(c1), len(c3))):
        if idx < len(c1):
            interleaved.append(c1[idx])
        if idx < len(c3):
            interleaved.append(c3[idx])
    merged, seen = [], set()
    for cand in interleaved:
        iri = cand['iri']
        if iri in seen:
            continue
        seen.add(iri)
        merged.append(cand)
        if len(merged) >= limit:
            break
    return merged
