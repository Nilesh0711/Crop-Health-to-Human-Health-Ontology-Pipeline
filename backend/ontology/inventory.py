"""Read-only, release-pinned candidate search for the potato production profile."""
from contextlib import closing
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import sqlite3

from backend.shared.storage import file_hash

from backend.ontology.inventory_policy import DATABASE, FORMULAS, HASHES, SIGNATURE


_TREND_VERB = r'(?:increased|decreased|declined|rose|fell|diminished)'


def _parallel_trend(claim):
    """Identify an anchored As-X-changed, Y-changed observation."""
    subject = claim.get('subject_source_mention') or claim['subject']
    obj = claim.get('object_source_mention') or claim['object']
    if not re.fullmatch(_TREND_VERB, claim['relation'].strip(), re.I):
        return False
    pattern = (rf'\bas\s+{re.escape(subject)}\s+{_TREND_VERB}\s*,'
               rf'[^.;,]*\b{re.escape(obj)}\b[^.;,]*\b{_TREND_VERB}\b')
    return bool(re.search(pattern, claim.get('source_evidence_span', ''), re.I))


def open_inventory():
    default = Path(__file__).resolve().parents[2] / 'analysis/single_stage/v4_pilot'
    root = Path(os.environ.get('POTATO_ONTOLOGY_INDEX_DIR', default)).resolve()
    try:
        stamps = tuple((p.stat().st_size, p.stat().st_mtime_ns) for p in (root / name for name in HASHES))
    except FileNotFoundError as error:
        raise RuntimeError(f'Missing frozen ontology inventory at {root}. Set POTATO_ONTOLOGY_INDEX_DIR to the directory containing {DATABASE} and {FORMULAS}.') from error
    return _load(root, stamps)


@lru_cache(maxsize=1)
def _load(root, stamps):
    for name, expected in HASHES.items():
        if file_hash(root / name) != expected:
            raise ValueError(f'Ontology inventory checksum mismatch for {root / name}')
    with (root / FORMULAS).open(encoding='utf-8') as stream:
        return Inventory(root / DATABASE, json.load(stream))


class Inventory:
    signature = SIGNATURE

    def __init__(self, path, formulas):
        self.path = path
        self.formulas = formulas
        self._semantic_checked = False
        self._semantic = None

    def semantic_index(self):
        if not self._semantic_checked:
            from backend.ontology.semantic_index import open_semantic_index
            self._semantic = open_semantic_index(self.path.parent)
            self._semantic_checked = True
        return self._semantic

    def connection(self):
        return closing(sqlite3.connect(self.path.as_uri() + '?mode=ro', uri=True))

    def relation_issue(self, reference, claim, bindings, iri, mapping):
        from backend.evaluation.endpoint_verifier import _relation_fallback_gate_rejection
        from backend.evaluation.extract_then_ground import _reference_for_bindings
        from backend.ontology.property_types import check_property_types, effective_property_restrictions
        from backend.ontology.usage import direct_property_allowed
        if claim.get('measured_comparison'):
            return 'Measured comparison retained as an observation.'
        causal_group = 'http://purl.obolibrary.org/obo/RO_0002410'
        parents = reference.terms.get(iri, {}).get('parent_properties', ())
        if (iri == causal_group or any(p.get('iri') == causal_group for p in parents)) and _parallel_trend(claim):
            return 'The source reports parallel changes across levels. A causal property requires an asserted effect.'
        if claim.get('proposed_relation_iri'):
            subj_iri = bindings.get('subject', {}).get('iri')
            obj_iri = bindings.get('object', {}).get('iri')
            if not (subj_iri and obj_iri):
                return 'Binary fixed property requires resolved subject and object endpoints.'
            if claim.get('passive_role_reversal_verified') is False:
                return 'Passive source wording was not reversed into forward subject -> object order.'
            if mapping == 'exact' and not direct_property_allowed(iri):
                return 'This upstream grouping property permits broader classification only.'
            with self.connection() as db:
                projected = _reference_for_bindings(db, reference, bindings)
            row = {'bindings': {**bindings, 'relation': {'iri': iri, 'mapping': mapping}}}
            status = check_property_types(row, projected)['status']
            reqs = effective_property_restrictions(projected, iri)
            requires_types = bool(reqs['domain'] or reqs['range'] or reqs['unsupported_domain'] or reqs['unsupported_range'])
            if status != 'compatible' and not (status == 'not_applicable' and not requires_types):
                return f'The proposed property requires verified compatible endpoint types ({status}).'
            if (bindings['subject'].get('mapping') == 'exact'
                    and bindings['object'].get('mapping') == 'exact'
                    and subj_iri == obj_iri):
                return 'Exact self-link on identical ontology classes requires review.'
            return None
        with self.connection() as db:
            projected = _reference_for_bindings(db, reference, bindings)
        if iri in reference.profile.get('retrieval_object_properties', ()):
            row = {'bindings': {**bindings, 'relation': {'iri': iri, 'mapping': mapping}}}
            status = check_property_types(row, projected)['status']
            reqs = effective_property_restrictions(projected, iri)
            requires_types = bool(reqs['domain'] or reqs['range'] or reqs['unsupported_domain'] or reqs['unsupported_range'])
            if status != 'compatible' and not (status == 'not_applicable' and not requires_types):
                return f'The retrieved property requires verified compatible endpoint types ({status}).'
        return _relation_fallback_gate_rejection(claim | {'bindings': bindings},
                                                {'relation_quote': claim.get('relation_trigger', '')},
                                                iri, mapping, projected)

    @staticmethod
    def _curie(iri):
        tail = iri.rsplit('/', 1)[-1]
        if iri.startswith('http://identifiers.org/hgnc/'):
            return f'HGNC:{tail}'
        if iri.startswith('http://identifiers.org/ncbigene/'):
            return f'NCBIGene:{tail}'
        if iri.startswith('https://swisslipids.org/rdf/') and '_' in tail:
            return tail.replace('_', ':', 1)
        return tail.replace('_', ':', 1) if '/obo/' in iri and '_' in tail else tail

    def _format_browser_term(self, raw):
        iri = raw['iri']
        exact = list(raw.get('exact_synonyms') or ())
        src = raw.get('source')
        return {
            'iri': iri,
            'id': self._curie(iri),
            'label': raw.get('label') or self._curie(iri),
            'kind': raw.get('kind', 'class'),
            'definition': raw.get('definition') or '',
            'synonyms': exact,
            'exact_synonyms': exact,
            'related_synonyms': [],
            'parents': [p for p in raw.get('parents', ()) if str(p).startswith(('http://', 'https://'))],
            'domain': [],
            'range': [],
            'deprecated': bool(raw.get('deprecated')),
            'origin': 'established_ontology',
            'selectable': True,
            'sources': [src] if src else [],
            'canonical_iri': iri,
        }

    def class_count(self):
        if not hasattr(self, '_class_count'):
            with self.connection() as db:
                self._class_count = int(db.execute("SELECT COUNT(*) FROM terms_fts WHERE kind='class'").fetchone()[0])
        return self._class_count

    def _primary_class_count(self):
        if not hasattr(self, '_primary_obo_count'):
            with self.connection() as db:
                self._primary_obo_count = int(db.execute(
                    "SELECT COUNT(*) FROM terms t JOIN terms_fts f USING (iri) "
                    "WHERE f.kind='class' AND t.priority=15 AND t.iri LIKE 'http://purl.obolibrary.org/obo/%' AND NOT t.obsolete"
                ).fetchone()[0])
        return self._primary_obo_count

    def get_term(self, iri):
        if not iri:
            return None
        with self.connection() as db:
            row = db.execute('SELECT info FROM terms WHERE iri=? AND NOT obsolete', (iri,)).fetchone()
        if not row:
            return None
        parsed = json.loads(row[0])
        if parsed.get('kind', 'class') != 'class':
            return None
        return self._format_browser_term(parsed)

    def lineage_paths(self, iri, max_depth=18, max_paths=6):
        fetched = {}
        with self.connection() as db:
            def fetch_raw(target):
                if target not in fetched:
                    r = db.execute('SELECT info FROM terms WHERE iri=? AND NOT obsolete', (target,)).fetchone()
                    fetched[target] = json.loads(r[0]) if r else None
                return fetched[target]

            def brief_step(target):
                raw = fetch_raw(target)
                return {
                    'iri': target,
                    'id': self._curie(target),
                    'label': (raw or {}).get('label') or self._curie(target),
                }

            def walk(cur, depth, seen):
                if depth >= max_depth or cur in seen:
                    return [[brief_step(cur)]]
                raw = fetch_raw(cur)
                if not raw:
                    return [[brief_step(cur)]]
                next_seen = seen | {cur}
                parents = [p for p in raw.get('parents', ()) if str(p).startswith(('http://', 'https://'))]
                if not parents:
                    return [[brief_step(cur)]]
                out = []
                for p in parents[:4]:
                    if p in next_seen:
                        continue
                    for sub in walk(p, depth + 1, next_seen):
                        out.append([*sub, brief_step(cur)])
                        if len(out) >= max_paths:
                            return out
                return out or [[brief_step(cur)]]

            if not fetch_raw(iri):
                return []
            return walk(iri, 0, set())

    def search_classes(self, query='', *, limit=50, offset=0):
        needle = (query or '').strip()
        with self.connection() as db:
            if not needle:
                primary_total = self._primary_class_count()
                if offset + limit <= primary_total:
                    rows = db.execute(
                        "SELECT t.info FROM terms t "
                        "WHERE t.priority=15 AND t.iri LIKE 'http://purl.obolibrary.org/obo/%' AND NOT t.obsolete "
                        "LIMIT ? OFFSET ?",
                        (limit, offset),
                    ).fetchall()
                    terms = [self._format_browser_term(json.loads(r[0])) for r in rows
                             if json.loads(r[0]).get('kind', 'class') == 'class']
                elif offset >= primary_total:
                    rem_offset = offset - primary_total
                    rows = db.execute(
                        "SELECT t.info FROM terms_fts f JOIN terms t USING (iri) "
                        "WHERE f.kind='class' AND NOT (t.priority=15 AND t.iri LIKE 'http://purl.obolibrary.org/obo/%' AND NOT t.obsolete) "
                        "LIMIT ? OFFSET ?",
                        (limit, rem_offset),
                    ).fetchall()
                    terms = [self._format_browser_term(json.loads(r[0])) for r in rows]
                else:
                    first_n = primary_total - offset
                    rows_a = db.execute(
                        "SELECT t.info FROM terms t "
                        "WHERE t.priority=15 AND t.iri LIKE 'http://purl.obolibrary.org/obo/%' AND NOT t.obsolete "
                        "LIMIT ? OFFSET ?",
                        (first_n, offset),
                    ).fetchall()
                    rows_b = db.execute(
                        "SELECT t.info FROM terms_fts f JOIN terms t USING (iri) "
                        "WHERE f.kind='class' AND NOT (t.priority=15 AND t.iri LIKE 'http://purl.obolibrary.org/obo/%' AND NOT t.obsolete) "
                        "LIMIT ? OFFSET 0",
                        (limit - first_n,),
                    ).fetchall()
                    terms = ([self._format_browser_term(json.loads(r[0])) for r in rows_a
                              if json.loads(r[0]).get('kind', 'class') == 'class']
                             + [self._format_browser_term(json.loads(r[0])) for r in rows_b])
                return {'terms': terms, 'total': self.class_count(), 'offset': offset, 'limit': limit}

            direct_iris = []
            if needle.startswith(('http://', 'https://')):
                direct_iris.append(needle)
            elif re.fullmatch(r'HGNC:\d+', needle, re.I):
                direct_iris.append(f"http://identifiers.org/hgnc/{needle.split(':', 1)[1]}")
            elif re.fullmatch(r'NCBIGene:\d+', needle, re.I):
                direct_iris.append(f"http://identifiers.org/ncbigene/{needle.split(':', 1)[1]}")
            elif re.fullmatch(r'[A-Za-z0-9_]+[:_]\d+', needle):
                prefix, num = needle.split(':', 1) if ':' in needle else needle.rsplit('_', 1)
                direct_iris.append(f'http://purl.obolibrary.org/obo/{prefix}_{num}')
                direct_iris.append(f'https://cropontology.org/rdf/{prefix}:{num}')
            elif re.fullmatch(r'CO_\d+:\d+', needle, re.I):
                direct_iris.append(f'https://cropontology.org/rdf/{needle}')

            if direct_iris:
                placeholders = ','.join('?' for _ in direct_iris)
                hits = db.execute(
                    f'SELECT info FROM terms WHERE iri IN ({placeholders}) AND NOT obsolete',
                    direct_iris,
                ).fetchall()
                terms = [self._format_browser_term(json.loads(r[0])) for r in hits if json.loads(r[0]).get('kind', 'class') == 'class']
                if terms:
                    return {'terms': terms[offset:offset + limit], 'total': len(terms), 'offset': offset, 'limit': limit}

            tokens = [t for t in re.findall(r'[a-z0-9]+', needle.casefold()) if t]
            if not tokens:
                return {'terms': [], 'total': 0, 'offset': offset, 'limit': limit}
            fts_query = ' AND '.join(f'"{t}"*' for t in tokens)
            total = int(db.execute(
                "SELECT COUNT(*) FROM terms_fts WHERE kind='class' AND terms_fts MATCH ?",
                (fts_query,),
            ).fetchone()[0])
            rows = db.execute(
                "SELECT t.info FROM terms_fts f JOIN terms t USING (iri) "
                "WHERE f.kind='class' AND terms_fts MATCH ? "
                "ORDER BY (lower(f.label) != ?), bm25(terms_fts, 10.0, 6.0, 1.0), f.label "
                "LIMIT ? OFFSET ?",
                (fts_query, needle.casefold(), limit, offset),
            ).fetchall()
            terms = [self._format_browser_term(json.loads(r[0])) for r in rows]
            return {'terms': terms, 'total': total, 'offset': offset, 'limit': limit}

    def ancestors(self, reference, iri):
        return list(self.lineage(reference, iri))

    @lru_cache(maxsize=16384)
    def lineage(self, reference, iri):
        from backend.evaluation.extract_then_ground import _reference_for_bindings
        from backend.ontology.property_types import (
            BFO_CONTINUANT,
            BFO_INDEPENDENT_CONTINUANT,
            BFO_MATERIAL_ENTITY,
            BFO_OCCURRENT,
            BFO_PROCESS,
            BFO_SDC,
            PATO_CHARACTERISTIC,
            class_ancestors,
        )
        with self.connection() as db:
            ref = _reference_for_bindings(db, reference, {'subject': {'iri': iri}, 'object': {'iri': None}})
            anc = set(class_ancestors(ref, iri))
        if 'http://purl.obolibrary.org/obo/TO_0000387' in anc:
            anc.update((PATO_CHARACTERISTIC, BFO_SDC, BFO_CONTINUANT))
        elif iri.startswith('https://cropontology.org/rdf/CO_'):
            label_low = ((ref.terms.get(iri) or {}).get('label') or '').strip().casefold()
            if not label_low.endswith((' - method', ' - scale', ' - computation', ' - measurement', ' - variable')):
                anc.update((PATO_CHARACTERISTIC, BFO_SDC, BFO_CONTINUANT))
        if 'http://purl.obolibrary.org/obo/PECO_0007359' in anc or 'http://purl.obolibrary.org/obo/PECO_0001001' in anc:
            anc.update((BFO_PROCESS, BFO_OCCURRENT))
        if iri.startswith('http://purl.obolibrary.org/obo/NCBITaxon_') and iri != 'http://purl.obolibrary.org/obo/NCBITaxon_1':
            anc.update((BFO_MATERIAL_ENTITY, BFO_INDEPENDENT_CONTINUANT, BFO_CONTINUANT))
        if 'http://purl.obolibrary.org/obo/HP_0000118' in anc:
            anc.update((PATO_CHARACTERISTIC, BFO_SDC, BFO_CONTINUANT))
        if 'http://purl.obolibrary.org/obo/FOODON_00002403' in anc or 'http://purl.obolibrary.org/obo/FOODON_00001002' in anc:
            anc.update((BFO_MATERIAL_ENTITY, BFO_INDEPENDENT_CONTINUANT, BFO_CONTINUANT))
        if 'http://purl.obolibrary.org/obo/CMO_0000000' in anc:
            anc.update((PATO_CHARACTERISTIC, BFO_SDC, BFO_CONTINUANT))
        if 'http://purl.obolibrary.org/obo/CHEBI_24431' in anc or iri.startswith('http://purl.obolibrary.org/obo/PR_'):
            anc.update((BFO_MATERIAL_ENTITY, BFO_INDEPENDENT_CONTINUANT, BFO_CONTINUANT))
        return tuple(sorted(anc))

    def candidates(self, reference, name, kind, evidence, definitions, *,
                   cultivar=None, treatment_iris=(), treatment_source=None, methods_context='', required_ancestors=()):
        from backend.evaluation.experimental_retrieval import (
            _expand_query_phrase,
            formula_lookup_candidates,
            fts5_bm25_candidates,
        )
        from backend.evaluation.ontology_lookup import (
            _COLLECTIVE_STRUCTURE_HEADS,
            _CONDITION_HEADS,
            _LIMITATION_TOKENS,
            _NUTRIENT_TOKENS,
            _TRAIT_HEADS,
            endpoint_candidates,
            words as _lookup_words,
        )
        from backend.evaluation.endpoint_verifier import (
            _candidate_quantity_or_analyte_conflict,
            _endpoint_scale_conflict,
        )
        from backend.evaluation.extract_then_ground import _ELEMENTAL_CONTEXT_RE
        from backend.ontology.property_types import (
            BFO_CONTINUANT,
            BFO_MATERIAL_ENTITY,
            BFO_OCCURRENT,
            BFO_PROCESS,
        )
        from backend.ontology.semantic_index import (
            EXCLUDED_DENSE_MULTIPLIER,
            EXCLUDED_DENSE_SOURCES,
            RRF_K,
            rrf_fuse,
        )
        from backend.stages.ontology_grounding.shortlists import _stem_words

        treatment_definition = (treatment_source or {}).get('text', '')
        evidence_pool = ' '.join((evidence, methods_context, treatment_definition))

        sem = self.semantic_index()
        expanded_variants = [v.strip().casefold() for v in _expand_query_phrase(name, definitions or ())]
        expanded_text = expanded_variants[-1] if expanded_variants else name
        expanded_set = set(expanded_variants)
        query = _stem_words(expanded_text)
        req_anc = set(required_ancestors or ())

        def conflicts(term, *, is_formula=False):
            return (_endpoint_scale_conflict(expanded_text, evidence, term)
                    or (None if is_formula else _candidate_quantity_or_analyte_conflict(
                        name, evidence_pool, term, treatment_definition=treatment_definition)))

        deferred = set(reference.profile.get('deferred_classes', ())) if reference is not None else set()

        def format_hit(iri, term, fallback_source=None, retrieval_channels=None):
            payload = (
                {key: term[key] for key in ('label', 'definition', 'exact_synonyms', 'parents') if term.get(key)}
                | {'iri': iri, 'kind': 'class',
                   'source': term.get('source') or (term.get('sources') or [None])[0] or fallback_source,
                   'inventory_signature': self.signature}
            )
            if retrieval_channels:
                payload['retrieval_channels'] = retrieval_channels
            return payload

        effective_kind = kind
        if not effective_kind:
            toks = set(_lookup_words(expanded_text))
            head_toks = set(_lookup_words(re.split(r'\b(?:in|of|with|under|from)\b', expanded_text, maxsplit=1, flags=re.I)[0]))
            clinical_bio_tokens = {
                'serum', 'plasma', 'blood', 'urine', 'urinary', 'circulating',
                'hormone', 'pth', 'fgf23', 'calcitriol', 'vitamin', 'calcium', 'carotid', 'bone',
            }
            if (head_toks & (_CONDITION_HEADS | {'inoculation', 'coinoculation',
                                                'treatments', 'coating', 'application', 'inoculant', 'inoculants',
                                                'intake', 'consumption', 'deprivation', 'supplementation', 'dietary'})) or (
                (head_toks & {'level', 'levels'}) and not (toks & clinical_bio_tokens)
            ) or (
                (head_toks & _LIMITATION_TOKENS) and (toks & _NUTRIENT_TOKENS)
            ):
                effective_kind = 'condition or exposure'
            elif (head_toks & (_TRAIT_HEADS | {'biomass', 'volume', 'rate', 'power', 'excretion',
                                               'thickness', 'density', 'biomarker', 'biomarkers', 'level', 'levels'})) or (
                toks & {'concentration', 'content', 'efficiency', 'yield', 'excretion', 'thickness', 'density'}
            ):
                effective_kind = 'trait or measurement'
            elif head_toks & _COLLECTIVE_STRUCTURE_HEADS:
                effective_kind = 'anatomy or part'
            elif head_toks & {'food', 'foods', 'product', 'products', 'milk', 'formula', 'dairy', 'cheese',
                              'meat', 'fish', 'bread', 'cereal', 'beverage', 'beverages', 'additive', 'additives'}:
                effective_kind = 'food'
            elif head_toks & {'disease', 'diseases', 'disorder', 'disorders', 'nephropathy', 'nephritis',
                              'nephrocalcinosis', 'fibrosis', 'calcification', 'hyperphosphatemia',
                              'hyperphosphaturia', 'damage', 'failure', 'impairment', 'inflammation',
                              'demineralization', 'mortality', 'toxicity'}:
                effective_kind = 'health outcome'

        has_bact_compound = bool(re.search(r'\b[a-z]+bacteri(?:a|um)\b', expanded_text, re.I))
        wants_process = bool(req_anc & {BFO_PROCESS, BFO_OCCURRENT})
        wants_continuant = bool(req_anc & {BFO_CONTINUANT, BFO_MATERIAL_ENTITY})

        with self.connection() as db:
            fml_hits = formula_lookup_candidates(self.formulas, name, limit=16)
            lex_hits = endpoint_candidates(db, reference, name, [evidence], definitions, limit=40)
            fts_hits = fts5_bm25_candidates(db, name, effective_kind, definitions, limit=40)
            sem_hits = sem.search(name, definitions=definitions, kind=effective_kind, limit=40, min_score=0.52) if sem is not None else []

            aux_channels = []
            if has_bact_compound:
                # Decompose biological *bacteria compounds (e.g. rhizobacteria -> bacteria / bacterial plant exposure).
                if effective_kind == 'condition or exposure' or wants_process:
                    aux_channels.append(('lexical', 0.85, endpoint_candidates(
                        db, reference, 'bacterial plant exposure', [evidence], definitions, limit=8)))
                    if sem is not None:
                        aux_channels.append(('dense_bge', 1.35, sem.search(
                            'bacterial plant exposure', kind='condition or exposure', limit=8, min_score=0.52)))
                if wants_continuant or (effective_kind != 'condition or exposure' and not wants_process):
                    aux_channels.append(('lexical', 0.85, endpoint_candidates(
                        db, reference, 'bacteria', [evidence], definitions, limit=10)))
                    aux_channels.append(('fts5', 0.65, fts5_bm25_candidates(
                        db, 'bacteria', 'organism', definitions, limit=10)))
            if (expanded_text.strip().casefold() == 'phosphorus'
                    and not wants_process and not _ELEMENTAL_CONTEXT_RE.search(evidence_pool)):
                # Bare nutritional "phosphorus" is withheld from elemental CHEBI:28659; include dietary phosphorus (CDNO:0000018).
                aux_channels.append(('lexical', 0.90, endpoint_candidates(
                    db, reference, 'dietary phosphorus', [evidence], definitions, limit=8)))
                aux_channels.append(('fts5', 0.70, fts5_bm25_candidates(
                    db, 'dietary phosphorus', 'chemical', definitions, limit=8)))
                if sem is not None:
                    aux_channels.append(('dense_bge', 1.50, sem.search(
                        'dietary phosphorus', kind='chemical', limit=8, min_score=0.52)))

            if effective_kind == 'condition or exposure':
                source_multipliers = {'chebi.owl.gz': 0.65, 'ncbitaxon.owl.gz': 0.65}
            elif effective_kind in {'trait or measurement', 'assay variable', 'biological process', 'anatomy or part', 'food'}:
                source_multipliers = {'ncbitaxon.owl.gz': 0.65}
            elif effective_kind == 'health outcome':
                source_multipliers = {'ncbitaxon.owl.gz': 0.65, 'chebi.owl.gz': 0.75}
            else:
                source_multipliers = {src: EXCLUDED_DENSE_MULTIPLIER for src in EXCLUDED_DENSE_SOURCES}
            fused = rrf_fuse([
                ('lexical', 0.90, lex_hits),
                ('fts5', 0.70, fts_hits),
                ('dense_bge', 1.60, sem_hits),
                *aux_channels,
            ], k=RRF_K, source_multipliers=source_multipliers)
            ordered_hits = sorted(
                fused.values(),
                key=lambda item: (-item['rrf_score'], item['iri']),
            )
            formula_iris = {h['iri'] for h in fml_hits}
            for fh in reversed(fml_hits):
                if fh['iri'] not in fused:
                    ordered_hits.insert(0, {'iri': fh['iri'], 'class_source': fh.get('class_source'), 'rrf_score': 0.0, 'ranks': {'chebi_formula': 1}})

            result = []
            retrieved = []
            rrf_scores = {}
            child_scores = {}
            scale_fallback_needed = False
            rejected_scale_score = 0.0
            for hit in ordered_hits:
                iri = hit['iri']
                if iri in deferred:
                    continue
                row = db.execute('SELECT info FROM terms WHERE iri=? AND NOT obsolete', (iri,)).fetchone()
                term = json.loads(row[0]) if row else (reference.terms.get(iri, {}) if reference is not None else {})
                if not term or term.get('deprecated') or term.get('kind', 'class') != 'class':
                    continue
                retrieved_term = term | {'iri': iri}
                retrieved.append(retrieved_term)
                child_scores[iri] = max(child_scores.get(iri, 0.0), hit['rrf_score'])
                scale_issue = _endpoint_scale_conflict(expanded_text, evidence, retrieved_term)
                if scale_issue and 'root system' in scale_issue:
                    scale_fallback_needed = True
                    rejected_scale_score = max(rejected_scale_score, hit['rrf_score'])
                if conflicts(retrieved_term, is_formula=(iri in formula_iris)):
                    continue
                rrf_scores[iri] = hit['rrf_score']
                channels = dict(hit.get('ranks', {}))
                if iri in formula_iris:
                    channels['chebi_formula'] = 1
                result.append(format_hit(iri, term, hit.get('class_source'), channels))

            accepted = {term['iri'] for term in result}
            if scale_fallback_needed:
                fb_hits = (
                    sem.search('root system morphology', kind='trait or measurement', limit=6, min_score=0.50)
                    if sem is not None else []
                )
                if not fb_hits:
                    for _, fb_iri, fb_info in db.execute(
                        "SELECT DISTINCT n.weight, n.iri, t.info FROM names n JOIN terms t USING (iri) "
                        "WHERE n.key IN ('root system morphology', 'root system morphology trait') AND NOT t.obsolete LIMIT 6"
                    ).fetchall():
                        fb_hits.append({'iri': fb_iri, 'class_source': json.loads(fb_info).get('source')})
                for idx, fb in enumerate(fb_hits):
                    fb_iri = fb['iri']
                    if fb_iri in deferred:
                        continue
                    fb_score = max(rejected_scale_score * max(0.55, 0.92 - 0.06 * idx), 2.40 / (RRF_K + 1 + idx))
                    if fb_iri in accepted:
                        rrf_scores[fb_iri] = max(rrf_scores.get(fb_iri, 0.0), fb_score)
                        continue
                    row = db.execute('SELECT info FROM terms WHERE iri=? AND NOT obsolete', (fb_iri,)).fetchone()
                    term = json.loads(row[0]) if row else (reference.terms.get(fb_iri, {}) if reference is not None else {})
                    if not term or term.get('deprecated') or term.get('kind', 'class') != 'class':
                        continue
                    if conflicts(term | {'iri': fb_iri}):
                        continue
                    rrf_scores[fb_iri] = fb_score
                    result.append(format_hit(fb_iri, term, fb.get('class_source'), {'dense_bge_scale_fallback': idx + 1}))
                    accepted.add(fb_iri)

            generic_parent_ns = frozenset({'BFO', 'COB', 'RO', 'IAO', 'CARO', 'CHEBI', 'NCBITaxon'})
            stop_parent_iris = frozenset({
                'http://purl.obolibrary.org/obo/PECO_0001001',  # plant exposure
                'http://purl.obolibrary.org/obo/PECO_0007359',  # plant experimental condition
                'http://purl.obolibrary.org/obo/TO_0000387',    # plant trait
                'http://purl.obolibrary.org/obo/GO_0008150',    # biological_process
                'http://purl.obolibrary.org/obo/PO_0025131',    # plant anatomical entity
                'http://purl.obolibrary.org/obo/PATO_0000001',  # quality
                'http://purl.obolibrary.org/obo/ENVO_01000254', # environmental system
                'http://purl.obolibrary.org/obo/FOODON_00002403', # food material
                'http://purl.obolibrary.org/obo/HP_0000001',    # All
                'http://purl.obolibrary.org/obo/HP_0000118',    # Phenotypic abnormality
                'http://purl.obolibrary.org/obo/MONDO_0000001', # disease
                'http://purl.obolibrary.org/obo/CMO_0000000',   # clinical measurement
                'http://purl.obolibrary.org/obo/OBA_0000001',   # biological attribute
            })
            parent_floor = 0.5 / (RRF_K + 25)
            parent_scores = {}
            parent_iris = []
            hop_decays = (0.88, 0.80, 0.74)

            def fetch_db_term(target_iri):
                row = db.execute('SELECT info FROM terms WHERE iri=? AND NOT obsolete', (target_iri,)).fetchone()
                if row:
                    return json.loads(row[0])
                return reference.terms.get(target_iri, {}) if reference is not None else {}

            def collect_parents(child_term, c_score, hop=0, seen_branch=None):
                if hop >= len(hop_decays):
                    return
                seen_branch = set(seen_branch or ()) | {child_term['iri']}
                child_ns = child_term['iri'].rsplit('/', 1)[-1].partition('_')[0]
                decay = hop_decays[hop]
                for parent in child_term.get('parents', ()):
                    if not str(parent).startswith(('http://', 'https://')) or parent in deferred or parent in seen_branch:
                        continue
                    parent_ns = parent.rsplit('/', 1)[-1].partition('_')[0]
                    if hop > 0 and (parent_ns != child_ns or parent_ns in generic_parent_ns):
                        continue
                    prop = decay * c_score if (parent_ns == child_ns and parent_ns not in generic_parent_ns and c_score > 0) else parent_floor
                    parent_scores[parent] = max(parent_scores.get(parent, 0.0), prop)
                    parent_iris.append(parent)
                    if (parent_ns == child_ns and parent_ns not in generic_parent_ns
                            and parent not in stop_parent_iris and hop + 1 < len(hop_decays)):
                        p_term = fetch_db_term(parent)
                        if p_term and not p_term.get('deprecated') and p_term.get('kind', 'class') == 'class':
                            collect_parents(p_term | {'iri': parent}, prop, hop + 1, seen_branch)

            seen_children = set()
            for hit_term in retrieved[:5]:
                c_iri = hit_term['iri']
                seen_children.add(c_iri)
                collect_parents(hit_term, child_scores.get(c_iri, 0.0))
            for dense_idx, s_hit in enumerate(sem_hits[:6]):
                s_iri = s_hit['iri']
                if s_iri in deferred or s_iri in seen_children:
                    continue
                seen_children.add(s_iri)
                s_term = fetch_db_term(s_iri)
                if not s_term or s_term.get('deprecated') or s_term.get('kind', 'class') != 'class':
                    continue
                s_score = max(child_scores.get(s_iri, 0.0), 1.60 / (RRF_K + 1 + dense_idx))
                collect_parents(s_term | {'iri': s_iri}, s_score)

            parent_set = set(parent_iris)
            preferred = [cultivar['species_iri']] if cultivar else []
            for iri in dict.fromkeys([*preferred, *treatment_iris, *parent_iris]):
                if iri in deferred:
                    continue
                if iri in accepted:
                    if iri in parent_scores and parent_scores[iri] > rrf_scores.get(iri, 0.0):
                        rrf_scores[iri] = parent_scores[iri]
                    continue
                row = db.execute('SELECT info FROM terms WHERE iri=? AND NOT obsolete', (iri,)).fetchone()
                term = json.loads(row[0]) if row else (reference.terms.get(iri, {}) if reference is not None else {})
                if not term or term.get('deprecated') or term.get('kind', 'class') != 'class':
                    continue
                if conflicts(term | {'iri': iri}):
                    continue
                rrf_scores[iri] = parent_scores.get(iri, parent_floor)
                result.append(format_hit(iri, term, retrieval_channels={'sqlite_parent': 1} if iri in parent_set else {'anchor': 1}))
                accepted.add(iri)

            contextual = set(preferred) | set(treatment_iris) | formula_iris

            def rank(term):
                iri = term['iri']
                if iri in contextual:
                    score = 1.0 + rrf_scores.get(iri, 0.0)
                else:
                    score = rrf_scores.get(iri, 0.0)
                    label_low = (term.get('label') or '').strip().casefold()
                    syn_lows = {
                        re.sub(r'\s*\((?:exact|narrow|broad|related)\)\s*$', '', s.strip().casefold())
                        for s in term.get('exact_synonyms', ()) if s
                    }
                    if label_low in expanded_set:
                        score += 0.050
                    elif syn_lows & expanded_set:
                        score += 0.025
                    prefix = iri.rsplit('/', 1)[-1].partition('_')[0]
                    lbl_stems = _stem_words(term.get('label', ''))
                    if ((iri in parent_set or prefix in {'PATO', 'TO', 'CMO', 'OBA'}
                            or (prefix == 'GO' and effective_kind != 'condition or exposure'))
                            and lbl_stems and lbl_stems <= query):
                        score += 0.036 if prefix in {'PATO', 'CMO', 'OBA'} else (0.022 if prefix == 'GO' else 0.016)
                if req_anc and reference is not None and score > 0 and req_anc <= set(self.lineage(reference, iri)):
                    score += 0.012
                return score

            scored = sorted(result, key=rank, reverse=True)
            if not req_anc or reference is None:
                return scored[:14]
            compatible = [t for t in scored if req_anc <= set(self.lineage(reference, t['iri'])) and rank(t) > 0][:6]
            kept = list(compatible)
            kept_iris = {t['iri'] for t in kept}
            for t in scored:
                if len(kept) >= 14:
                    break
                if t['iri'] not in kept_iris:
                    kept.append(t)
                    kept_iris.add(t['iri'])
            return sorted(kept, key=rank, reverse=True)[:14]

    @staticmethod
    def _production_admission_issue(endpoint, term, ns, kind, db):
        from backend.evaluation.endpoint_verifier import (
            _COLLECTIVE_PO_PARENTS,
            _COLLECTIVE_STRUCTURE_RE,
            _COUNT_SUBSET_RE,
            _DEFICIENCY_ONLY_RE,
            _EFFICIENCY_CLASS_RE,
            _EFFICIENCY_ENDPOINT_RE,
            _GENERIC_UPPER_BLOCK_IDS,
            _LIMITED_EXPOSURE_RE,
            _MEASUREMENT_ONLY_RE,
            _P_ADDITION_EVIDENCE_RE,
            _PECO_LIMITED_PHOSPHATE_IRI,
            _PECO_SUPPLEMENT_IRI,
            _candidate_quantity_or_analyte_conflict,
            _endpoint_scale_conflict,
        )
        from backend.evaluation.extract_then_ground import (
            _CULTIVAR_OR_GENOTYPE_RE,
            _DISTINCT_COORDINATED_TRAIT_RE,
            _is_blocked_elemental_chemical,
        )

        iri = term['iri']
        local_id = iri.rsplit('/', 1)[-1]
        name = endpoint['endpoint']
        expanded = endpoint.get('expanded_endpoint') or name

        if _MEASUREMENT_ONLY_RE.fullmatch(name.strip()):
            return 'Hard gate: pure measurement/percentage span cannot be mapped as an ontology class.'
        if _COUNT_SUBSET_RE.search(expanded):
            return 'Hard gate: subset count phrase cannot map as an unrestricted ontology trait class.'
        if local_id in _GENERIC_UPPER_BLOCK_IDS:
            return f'Hard gate: generic upper ontology class {local_id} is too broad for experimental endpoint grounding.'

        evidence_pool = ' '.join(
            t for t in (endpoint.get('evidence_span', ''), endpoint.get('local_clause', ''), endpoint.get('methods_context', '')) if t
        )

        if kind == 'condition or exposure':
            if iri == _PECO_LIMITED_PHOSPHATE_IRI:
                if not _LIMITED_EXPOSURE_RE.search(expanded):
                    return 'Hard gate: PECO:0001046 (limited phosphate exposure) requires limited, low, or absent exposure wording.'
            elif iri == _PECO_SUPPLEMENT_IRI:
                if (_DEFICIENCY_ONLY_RE.search(expanded)
                        and not re.search(r'\b(?:suppl\w*|appli\w*|add\w*|level|levels|expos\w*|treatment\w*)\b', expanded, re.I)):
                    return 'Hard gate: deficiency/scarcity/shortage alone does not establish phosphorus supplementation (PECO:0007397).'
                if endpoint.get('treatment_definition') and not _P_ADDITION_EVIDENCE_RE.search(endpoint['treatment_definition']):
                    return 'Hard gate: experimental phosphorus arm lacks treatment evidence that phosphorus was added (PECO:0007397).'

        local_evidence = ' '.join(t for t in (endpoint.get('evidence_span', ''), endpoint.get('local_clause', '')) if t)
        if conflict := _endpoint_scale_conflict(expanded, local_evidence, term):
            return conflict

        if _EFFICIENCY_ENDPOINT_RE.search(expanded) and not _EFFICIENCY_CLASS_RE.search(
            f"{term.get('label', '')} {term.get('definition', '')}"
        ):
            return (
                f"Hard gate: efficiency endpoint '{expanded}' cannot collapse onto non-efficiency "
                f"class '{term.get('label') or iri}'."
            )

        if len({m.group(0).casefold() for m in _DISTINCT_COORDINATED_TRAIT_RE.finditer(expanded)}) >= 2:
            return f"Hard gate: bundled endpoint '{expanded}' combines multiple distinct traits."

        if conflict := _candidate_quantity_or_analyte_conflict(
            expanded, evidence_pool, term, treatment_definition=endpoint.get('treatment_definition', ''),
        ):
            return conflict

        if kind == 'plant part' and _COLLECTIVE_STRUCTURE_RE.search(expanded):
            rec = db.execute('SELECT info FROM terms WHERE iri=?', (iri,)).fetchone()
            db_term = json.loads(rec[0]) if rec else {}
            label = str(term.get('label') or db_term.get('label', ''))
            definition = str(term.get('definition') or db_term.get('definition', ''))
            cand_parents = set(term.get('parents') or ())
            db_parents = set(db_term.get('parents') or ())
            is_collective = (
                'system' in label.casefold()
                or 'network' in label.casefold()
                or 'collective' in label.casefold()
                or 'collective' in definition.casefold()
                or bool(db_parents & _COLLECTIVE_PO_PARENTS)
                or any('collective' in str(p).casefold() for p in cand_parents)
            )
            if not is_collective:
                return 'Hard gate: collective structure mention cannot map to a non-collective anatomical part (meronymy is not subsumption).'

        if kind == 'chemical':
            rec = db.execute('SELECT info FROM terms WHERE iri=?', (iri,)).fetchone()
            db_term = json.loads(rec[0]) if rec else {'label': term.get('label', '')}
            if _is_blocked_elemental_chemical(iri, db_term, name, [endpoint['evidence_span']]):
                return 'Generic element mention lacks a specified chemical form; atomic CHEBI classes are withheld.'

        if kind == 'organism' and _CULTIVAR_OR_GENOTYPE_RE.search(expanded):
            return 'Hard gate: cultivar/genotype mention cannot collapse to a species-level taxon.'

        return None

    def admit(self, item, role, decision, term, methods_context=''):
        """Apply the tested scientific admission rules to both kinds of finding."""
        from backend.evaluation.endpoint_verifier import _enforce_broader_if_qualified
        from backend.evaluation.extract_then_ground import TYPE_SOURCES, _namespace

        if decision['mapping'] not in {'exact', 'broader'}:
            return
        cultivar = item.get(role + '_cultivar_source')
        if cultivar and cultivar['species_iri'] == term['iri']:
            decision.update(mapping='broader', admission_reason='The cited cultivar belongs to this crop species.')
            return
        from backend.evaluation.experimental_retrieval import parse_chemical_formula
        formula = parse_chemical_formula(item[role])
        if formula and formula[1] is not None and decision['mapping'] == 'exact':
            entries = self.formulas['by_exact'].get(f'{formula[0]}|chg={formula[1]}', ())
            if not any(entry['iri'] == term['iri'] for entry in entries):
                decision.update(mapping='unresolved', iri=None, canonical_iri=None, label=None,
                                admission_reason='The selected class does not verify the stated formula and charge.')
                return
        ns = _namespace(term['iri'])
        hint = (item.get('extraction_type_hints') or {}).get(role)
        kind = hint if hint in TYPE_SOURCES and TYPE_SOURCES[hint] else next(
            (key for key, namespaces in TYPE_SOURCES.items() if ns in namespaces), 'other')
        endpoint = {'endpoint': item[role], 'extracted_type': kind,
                    'evidence_span': item.get('source_evidence_span', ''), 'methods_context': methods_context}
        setup = item.get(role + '_treatment_source')
        if setup:
            if ns not in TYPE_SOURCES['condition or exposure']:
                decision.update(mapping='unresolved', iri=None, canonical_iri=None, label=None,
                                admission_reason='Study arm code names a source-defined treatment condition.')
                return
            endpoint['methods_context'] = methods_context + ' ' + setup['text']
            endpoint['treatment_definition'] = setup['text']
            if decision['mapping'] == 'exact':
                decision.update(mapping='broader', admission_reason='The exposure class omits the study-specific treatment level.')
        proposal = {'decision': decision['mapping'], 'candidate_alias': term['iri'],
                    'type_challenged': False, 'revised_type': None}
        with self.connection() as db:
            issue = self._production_admission_issue(endpoint, term, ns, kind, db)
        if issue:
            decision.update(mapping='unresolved', iri=None, canonical_iri=None, label=None, admission_reason=issue)
        else:
            mapping, omitted = _enforce_broader_if_qualified(endpoint, proposal, term)
            if kind in {'trait or measurement', 'assay variable'}:
                from backend.evaluation.comparison_pilot import ground_trait_location_components
                with self.connection() as db:
                    components = ground_trait_location_components(item[role], decision | {'mapping': mapping}, db,
                                                                 source_text=endpoint['evidence_span'], methods_context=methods_context)
                if term['iri'] == 'http://purl.obolibrary.org/obo/PATO_0001029' and components.get('status') == 'partial_broader':
                    components = {**components, 'grounding_completeness': 'complete'}
                decision['components'] = components
                if components['status'] == 'held_bundled_organs':
                    decision.update(mapping='unresolved', iri=None, canonical_iri=None, label=None,
                                    admission_reason=components['summary'])
                    return
                if components.get('trait', {}).get('mapping') == 'broader':
                    mapping = 'broader'
                if mapping != decision['mapping'] and not omitted:
                    omitted = [components['summary']]
            if omitted:
                decision.update(mapping=mapping, admission_reason='Class omits ' + ', '.join(omitted))
