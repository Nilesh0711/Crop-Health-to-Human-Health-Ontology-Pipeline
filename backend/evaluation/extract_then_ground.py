"""SPIRES-style typed extraction followed by local ontology annotation."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import ChainMap, deque
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace

from backend.domain.extraction_review import extraction_warnings, mapping_meaning_warnings
from backend.evaluation.ontology_lookup import (
    _expand,
    _source_aliases,
    endpoint_candidates,
    open_lookup,
    verify_narrow_synonym_in_owl,
    words,
)
from backend.evaluation.single_stage import ROLES, local_sentences, relation_evidence_issue, source_targets
from backend.ontology.property_types import check_property_types
from backend.providers.structured_output import SchemaValidationError
from backend.shared.artifacts import save_json, save_rows
from backend.shared.cancellation import check_cancelled
from backend.shared.storage import stable_hash
from backend.shared.text import head_token_matches, source_surface_mention
from backend.stages.claim_extraction.anchoring import endpoint_anchor
from backend.stages.claim_extraction.targeting import check_target_evidence


POLICY = 'typed-extraction-local-ontology-v3'
_MODULE_DIR = Path(__file__).resolve().parent
SCHEMA_PATH = _MODULE_DIR / 'typed_extraction_schema.json'
PROMPT_PATH = _MODULE_DIR / 'typed_extraction_prompt.md'

TYPE_SOURCES = {
    'organism': {'NCBITaxon'},
    'plant part': {'PO'},
    'trait or measurement': {'TO', 'PATO', 'CO_330', 'CMO', 'OBA'},
    'assay variable': {'CO_330', 'CMO', 'OBA'},
    'chemical': {'CHEBI', 'PR'},
    'condition or exposure': {'PECO', 'ECTO', 'ENVO'},
    'biological process': {'GO'},
    'food': {'FOODON'},
    'food composition': {'CDNO'},
    'health outcome': {'HP', 'MONDO'},
    'other': set(),
}

_UNICODE_HYPHENS = str.maketrans({'\u2010': '-', '\u2011': '-', '\u2012': '-', '\u2013': '-', '\u2014': '-', '\u2212': '-'})
_QUALIFIED_RELATION = re.compile(
    r'\b(?:may|might|could|can|not|no|never|possibly|potentially|likely|hypothes\w*|suggest\w*|expected)\b',
    re.I,
)
_NULL_EFFECT_RE = re.compile(
    r'\b(?:not\b.{0,55}\bsignifican\w*|no\s+(?:statistically\s+)?significant|non[-\s]?significant|no\s+(?:such\s+)?(?:relationship|correlation|association)s?|not\s+considerable)\b',
    re.I,
)
_UNCERTAIN_CUE_RE = re.compile(
    r'\b(?:may|might|could|possibly|potentially|likely|perhaps|hypothes\w*|suggest\w*|expect(?:ed|s)?\s+to)\b',
    re.I,
)
_NEGATION_CUE_RE = re.compile(
    r"\b(?:not|never|none|neither|nor|without|cannot|can't|couldn't|didn't|doesn't|don't|failed\s+to|lack(?:ed|s|ing)?|absen\w*)\b",
    re.I,
)
_DIRECTIONAL_UPGRADE_RE = re.compile(r'\b(?:improv\w*|increas\w*|enhanc\w*|reduc\w*|decreas\w*|promot\w*)\b', re.I)
_DIRECTIONAL_EVIDENCE_RE = re.compile(
    r'\b(?:improv\w*|increas\w*|enhanc\w*|reduc\w*|decreas\w*|promot\w*|higher|lower|more|less|great\w*|rais\w*|elevat\w*|declin\w*|diminish\w*|positive|negative)\b',
    re.I,
)
_NEUTRAL_AFFECT_RE = re.compile(r'\b(?:affect\w*|influenc\w*)\b', re.I)
_BUNDLED_BOTH_RE = re.compile(r'\bboth\s+\S+(?:\s+\S+){0,3}\s+and\s+\S+', re.I)
_DISTINCT_COORDINATED_TRAIT_RE = re.compile(
    r'\b(?:root\s+length|root\s+surface\s+area|root\s+volume|root\s+diameter|'
    r'root\s+biomass|shoot\s+biomass|root\s+dry\s+weight|shoot\s+dry\s+weight|'
    r'shoot\s+dm|root\s+dm|root\s+efficiency|specific\s+p\s+uptake|total\s+p\s+uptake|'
    r'tuber\s+yield|tuber\s+quality|starch\s+quality|phosphorus\s+content|p\s+content|'
    r'phosphorus\s+uptake|p\s+uptake)\b',
    re.I,
)
_SIGNIFICANCE_WORD_RE = re.compile(r'\bsignifican\w*\b', re.I)
_POS_POLARITY_RE = re.compile(
    r'\b(?:positive(?:ly)?|increas\w*|improv\w*|enhanc\w*|promot\w*|elevat\w*|augment\w*|amplif\w*)\b',
    re.I,
)
_NEG_POLARITY_RE = re.compile(
    r'\b(?:negative(?:ly)?|decreas\w*|reduc\w*|inhibit\w*|suppress\w*|declin\w*|diminish\w*)\b',
    re.I,
)
_NECESSITY_RE = re.compile(r'\b(?:requir\w*|necessar\w*|essential)\b', re.I)
_COMPARATIVE_THAN_RE = re.compile(r'\b(?:less|more|higher|lower|greater|smaller)\s+than\b', re.I)
_PASSIVE_BY_RE = re.compile(
    r'\b(?:was|were|is|are|been|be|being)\s+(?:[A-Za-z-]+\s+){0,2}[A-Za-z-]+(?:ed|en)\s+(?:by|with)\b',
    re.I,
)
_SUBORDINATE_SPLIT_RE = re.compile(
    r'\s*(?:;\s*|,\s+(?:showing\s+that|indicating\s+that|whereas|while|although|though|but)\s+)',
    re.I,
)
_CLAUSE_PREDICATE_BOUNDARY_RE = re.compile(
    r'(?:;|,\s+(?:showing\s+that|indicating\s+that|whereas|while|although|though|but)\b|'
    r'\b(?:and|but|while|whereas)\s+'
    r'(?:(?:was|were|is|are|has|have|had|did|does|do|can|could|may|might|will|would)\s+)?'
    r'(?:(?:positively|negatively|significantly|strongly|weakly|also|only|not)\s+)*'
    r'(?:measured|observed|correlated|associated|affected|increased|decreased|reduced|improved|enhanced|'
    r'required|needed|transported|produced|regulated|remained|differed|showed|indicated|caused|'
    r'influenced|had|was|were|is|are)\b)',
    re.I,
)

_CULTIVAR_OR_GENOTYPE_RE = re.compile(
    r"(?:\b(?:cv\.?|cultivar|var\.?|variety|subsp\.?|ssp\.?|genotype\w*|line\w*|accession\w*|hybrid\w*|ecotype\w*)\b|'[A-Z][^']+')",
    re.I,
)
_ELEMENTAL_CONTEXT_RE = re.compile(r'\b(?:atom|atomic|elemental)\b', re.I)
_GENERIC_ELEMENT_CHEM_IDS = frozenset({'CHEBI_28659', 'CHEBI_30207', 'CHEBI_35895'})
_SUPERCLASS_STOP_IDS = frozenset({
    'BFO_0000001', 'BFO_0000002', 'BFO_0000003', 'BFO_0000004', 'BFO_0000015',
    'BFO_0000019', 'BFO_0000020', 'BFO_0000040', 'PATO_0000001', 'TO_0000387',
    'GO_0008150', 'NCBITaxon_1', 'CARO_0000000',
})

_RO_NECESSITY_POSITIVE = 'http://purl.obolibrary.org/obo/RO_0002304'
_RO_NEGATIVE_EFFECT = 'http://purl.obolibrary.org/obo/RO_0002305'
_RO_CORRELATED_WITH = 'http://purl.obolibrary.org/obo/RO_0002610'

_EXPLICIT_CORRELATION_RE = re.compile(
    r'(?:\b(?:(?:positively|negatively|significantly|strongly|weakly|modestly|statistically)\s+)?'
    r'correlat(?:ed|es|e)\s+with\b'
    r'|\b(?:had|has|have|show(?:s|ed)?|display(?:s|ed)?|exhibit(?:s|ed)?|found|observed|demonstrat(?:e|es|ed))\s+'
    r'(?:an?\s+)?(?:(?:positive|negative|significant|strong|weak|moderate|modest|linear|statistical|overall)\s+){0,2}'
    r'correlations?\s+(?:with|between)\b)',
    re.I,
)
_NEGATIVE_EFFECT_RE = re.compile(
    r'\b(?:had|has|have|exerts?|exerted|shows?|showed)\s+(?:an?\s+)?(?:overall\s+)?negative\s+(?:effect|impact)\s+on\b',
    re.I,
)
_DIRECTIONAL_CAUSAL_IRIS = frozenset({_RO_NECESSITY_POSITIVE, _RO_NEGATIVE_EFFECT})

_PROPERTY_QUOTE_CUES = {
    'BFO_0000050': re.compile(r'\bparts?\s+of\b', re.I),
    'BFO_0000051': re.compile(r'\bhas\s+parts?\b', re.I),
    'RO_0000086': re.compile(r'\b(?:has\s+qualit\w*|possess\w*)\b', re.I),
    'RO_0000052': re.compile(r'\b(?:characteristic|quality)\s+of\b', re.I),
    'RO_0001025': re.compile(r'\blocat\w*\s+in\b', re.I),
    'RO_0000056': re.compile(r'\bparticipat\w*\s+in\b', re.I),
    'RO_0000057': re.compile(r'\bhas\s+participants?\b', re.I),
    'RO_0002020': re.compile(r'\btransport\w*\b', re.I),
    'RO_0002233': re.compile(r'\bhas\s+input\b', re.I),
    'RO_0002234': re.compile(r'\bhas\s+output\b', re.I),
    'RO_0002411': re.compile(r'\bcausal\w*\s+upstream\b', re.I),
    'RO_0002304': _NECESSITY_RE,
    'RO_0002305': re.compile(r'\b(?:negative\s+(?:effect|impact)|causally\s+upstream\s+of,\s+negative\s+effect)\b', re.I),
    'RO_0002211': re.compile(r'\bregulat\w*\b', re.I),
    'RO_0002213': re.compile(r'\bpositively\s+regulat\w*\b', re.I),
    'RO_0002212': re.compile(r'\bnegatively\s+regulat\w*\b', re.I),
    'RO_0003000': re.compile(r'\bproduc\w*\b', re.I),
    'RO_0002610': re.compile(r'\bcorrelat\w*\b', re.I),
    'RO_0002215': re.compile(r'\bcapable\s+of\b', re.I),
    'RO_0002596': re.compile(r'\bcapable\s+of\s+regulat\w*\b', re.I),
    'RO_0002566': re.compile(r'\bcausal\w*\s+influenc\w*\b', re.I),
    'RO_0002559': re.compile(r'\bcausal\w*\s+influenced\s+by\b', re.I),
}
_ACTIVE_VERBAL_PROPERTY_IDS = frozenset({
    'RO_0002020', 'RO_0003000', 'RO_0002211', 'RO_0002212', 'RO_0002213', 'RO_0002566',
})
_NOMINALIZATION_PATTERNS = {
    'RO_0002020': (re.compile(r'\btransport\s+of\b', re.I), re.compile(r'\b(?:transports|transported|transporting)\b', re.I)),
    'RO_0003000': (re.compile(r'\bproduction\s+of\b', re.I), re.compile(r'\b(?:produces|produced|producing)\b', re.I)),
    'RO_0002211': (re.compile(r'\bregulation\s+of\b', re.I), re.compile(r'\b(?:regulates|regulated|regulating)\b', re.I)),
    'RO_0002212': (re.compile(r'\bnegative\s+regulation\s+of\b', re.I), re.compile(r'\bnegatively\s+(?:regulates|regulated)\b', re.I)),
    'RO_0002213': (re.compile(r'\bpositive\s+regulation\s+of\b', re.I), re.compile(r'\bpositively\s+(?:regulates|regulated)\b', re.I)),
}

_GRAMMATICAL_RELATION_REWRITES = (
    (re.compile(r'^(?:is|are|was|were)\s+'), ''),
    (re.compile(r'\bparts of\b'), 'part of'),
    (re.compile(r'\bhas parts\b'), 'has part'),
    (re.compile(r'\bhas qualities\b'), 'has quality'),
    (re.compile(r'\bhas participants\b'), 'has participant'),
    (re.compile(r'\bparticipated in\b'), 'participates in'),
    (re.compile(r'\bparticipate in\b'), 'participates in'),
    (re.compile(r'\btransported\b'), 'transports'),
    (re.compile(r'\bproduced\b'), 'produces'),
    (re.compile(r'\bpositively regulated\b'), 'positively regulates'),
    (re.compile(r'\bnegatively regulated\b'), 'negatively regulates'),
    (re.compile(r'\bregulated\b'), 'regulates'),
    (re.compile(r'\bcausally influenced by\b'), 'causally influenced by'),
    (re.compile(r'\bcausally influenced\b'), 'causally influences'),
)


def load_extraction_schema():
    schema = json.loads(SCHEMA_PATH.read_text(encoding='utf-8'))
    claim_props = schema['properties']['claims']['items']['properties']
    schema_types = claim_props['subject_type']['enum']
    if set(schema_types) != set(TYPE_SOURCES) or schema_types != claim_props['object_type']['enum']:
        raise ValueError('typed_extraction_schema.json endpoint types drifted from TYPE_SOURCES.')
    return schema


def render_field_template(schema):
    claim = schema['properties']['claims']['items']
    lines = ['Root JSON object: {"claims": [<claim>, ...]}', 'Each claim object fields:']
    ordered = [*claim['required'], *(k for k in claim['properties'] if k not in claim['required'])]
    for name in ordered:
        spec = claim['properties'][name]
        req = 'required' if name in claim['required'] else 'optional'
        desc = spec.get('description', '')
        if 'enum' in spec:
            lines.append(f"- {name} ({req}; enum: {' | '.join(spec['enum'])}): {desc}")
        elif name == 'qualifiers':
            keys = ', '.join(spec['properties'])
            lines.append(f'- {name} ({req}; object with nullable string keys: {keys}): {desc}')
        elif 'properties' in spec:
            sub = ', '.join(spec['required'])
            lines.append(f'- {name} ({req}; object with {{{sub}}}): {desc}')
        else:
            lines.append(f"- {name} ({req}; {spec.get('type', 'value')}): {desc}")
    return '\n'.join(lines)


def extraction_request(reference):
    schema = load_extraction_schema()
    template = PROMPT_PATH.read_text(encoding='utf-8')
    prompt = (
        template
        .replace('{research_scope}', reference.extraction_instructions().strip())
        .replace('{field_template}', render_field_template(schema))
    )
    return prompt, schema


def _namespace(iri):
    return re.split('[_:]', iri.rsplit('/', 1)[-1], maxsplit=1)[0] if '/CO_330:' not in iri else 'CO_330'


def _normalize_hyphens(text):
    return ' '.join(unicodedata.normalize('NFKC', str(text or '')).translate(_UNICODE_HYPHENS).split())


def _exact_name(text, kind):
    name = _normalize_hyphens(text)
    if kind == 'chemical' and re.fullmatch(r'[A-Za-z0-9+().\- ]{1,12}', name) and (
            any(character.isdigit() for character in name) or len(name) <= 3):
        return name
    return name.casefold()


def _trait_name_keys(text, kind):
    keys = {_exact_name(text, kind)}
    if kind == 'trait or measurement':
        stripped = re.sub(r'\s+trait$', '', text.strip(), flags=re.I)
        if stripped and stripped != text.strip():
            keys.add(_exact_name(stripped, kind))
    return keys


def _is_blocked_elemental_chemical(iri, term, name, evidence):
    local_id = iri.rsplit('/', 1)[-1]
    label = str(term.get('label') or '').casefold()
    is_elemental = local_id in _GENERIC_ELEMENT_CHEM_IDS or label.endswith(' atom')
    if not is_elemental:
        return False
    combined = f"{name} {' '.join(evidence)}"
    return not _ELEMENTAL_CONTEXT_RE.search(combined)


def _singular_variants(text):
    tokens = text.split()
    if len(tokens) == 0:
        return ()
    last = tokens[-1]
    out = []
    if len(last) >= 5 and last.endswith('ies') and last[:-3].isalpha():
        out.append(' '.join([*tokens[:-1], last[:-3] + 'y']))
    elif len(last) >= 4 and last.endswith('s') and not last.endswith(('ss', 'us', 'is')):
        base = last[:-1]
        if head_token_matches(last.casefold(), base.casefold()):
            out.append(' '.join([*tokens[:-1], base]))
    return tuple(out)


def _exact_lookup_forms(name, kind, aliases):
    raw = _normalize_hyphens(name)
    expanded, hints = _expand(raw, aliases)
    expanded = _normalize_hyphens(expanded)
    seed_forms = [raw] + ([expanded] if expanded != raw else [])
    forms = list(seed_forms)

    for form in tuple(forms):
        m = re.match(r'^(.+?)\s*\(([A-Za-z][A-Za-z0-9-]{0,10})\)$', form)
        if m and any(c.isupper() for c in m[2]):
            forms.append(m[1].strip())

    if kind == 'organism' and not _CULTIVAR_OR_GENOTYPE_RE.search(raw):
        for form in tuple(forms):
            m = re.match(r'^[^()]+\(\s*([A-Z][a-z]{2,}\s+[a-z][a-z0-9-]{2,}(?:\s*,?\s*L\.?)?)\s*\)$', form)
            if m:
                forms.append(re.sub(r'(?:\s*,?\s*L\.?)$', '', m[1]).strip())
            no_auth = re.sub(r'^([A-Z][a-z]{2,}\s+[a-z][a-z0-9-]{2,})(?:\s*,?\s*L\.?)$', r'\1', form)
            if no_auth != form:
                forms.append(no_auth)

    if kind != 'chemical':
        for form in tuple(forms):
            no_article = re.sub(r'^(?:a|an|the)\s+', '', form, flags=re.I)
            if no_article != form:
                forms.append(no_article)
        for form in tuple(forms):
            forms.extend(_singular_variants(form))

    deduped = list(dict.fromkeys(f for f in forms if f))
    return deduped, hints


def _db_directory(db):
    try:
        for _, name, filename in db.execute('PRAGMA database_list').fetchall():
            if name == 'main' and filename:
                return Path(filename).parent
    except Exception:
        pass
    return None


def _namespace_matches_exact(db, sources, keys, kind, evidence, name):
    matches, blocked_chem = {}, False
    for key in sorted(keys):
        for iri, encoded in db.execute("""
                SELECT DISTINCT n.iri, t.info FROM names n JOIN terms t USING (iri)
                WHERE n.key=? AND n.weight IN (0,1) AND NOT t.obsolete
                """, (key,)):
            if _namespace(iri) not in sources:
                continue
            term = json.loads(encoded)
            if kind == 'chemical' and _is_blocked_elemental_chemical(iri, term, name, evidence):
                blocked_chem = True
                continue
            names = set()
            for label in [term['label'], *term['exact_synonyms']]:
                names.update(_trait_name_keys(label, kind))
            if keys[key] & names:
                matches[iri] = term
    return matches, blocked_chem


def _narrow_synonym_broader(db, sources, keys, kind):
    matches = {}
    lookup_dir = _db_directory(db)
    for key in sorted(keys):
        if len(key.split()) < 2:
            continue
        for iri, encoded in db.execute("""
                SELECT DISTINCT n.iri, t.info FROM names n JOIN terms t USING (iri)
                WHERE n.key IN (?, ?) AND n.weight=5 AND NOT t.obsolete
                """, (key, f'{key} narrow')):
            if _namespace(iri) not in sources:
                continue
            term = json.loads(encoded)
            verified = verify_narrow_synonym_in_owl(lookup_dir, iri, term.get('source', ''), keys.keys())
            if verified:
                matches[iri] = (term, verified)
    if kind == 'trait or measurement':
        ref_traits = {iri: val for iri, val in matches.items() if _namespace(iri) in {'TO', 'PATO'}}
        matches = ref_traits or matches
    return matches


def _superclass_broader(db, sources, keys, kind):
    specific = {}
    for key in sorted(keys):
        for iri, encoded in db.execute("""
                SELECT DISTINCT n.iri, t.info FROM names n JOIN terms t USING (iri)
                WHERE n.key=? AND n.weight IN (0,1) AND NOT t.obsolete
                """, (key,)):
            term = json.loads(encoded)
            names = set()
            for label in [term['label'], *term['exact_synonyms']]:
                names.update(_trait_name_keys(label, kind))
            if keys[key] & names:
                specific[iri] = term
    if len(specific) != 1:
        return None
    child_iri, child_term = next(iter(specific.items()))
    if _namespace(child_iri) in sources:
        return None
    visited = {child_iri}
    queue = deque((parent, 1) for parent in child_term.get('parents', ()))
    found_depth = None
    closest = {}
    while queue:
        iri, depth = queue.popleft()
        if found_depth is not None and depth > found_depth:
            break
        if iri in visited:
            continue
        visited.add(iri)
        row = db.execute('SELECT info FROM terms WHERE iri=? AND NOT obsolete', (iri,)).fetchone()
        if not row:
            continue
        term = json.loads(row[0])
        local_id = iri.rsplit('/', 1)[-1]
        if _namespace(iri) in sources and local_id not in _SUPERCLASS_STOP_IDS:
            found_depth = depth
            closest[iri] = term
            continue
        for parent in term.get('parents', ()):
            if parent not in visited:
                queue.append((parent, depth + 1))
    if len(closest) == 1:
        p_iri, p_term = next(iter(closest.items()))
        child_id = child_iri.rsplit('/', 1)[-1]
        return {
            'iri': p_iri,
            'label': p_term['label'],
            'mapping': 'broader',
            'reason': f"Identified specific class {child_term['label']} ({child_id}) subsumed by permitted superclass {p_term['label']}.",
            'provenance': {'class_source': p_term.get('source', ''), 'specific_class_source': child_term.get('source', '')},
        }
    return None


def resolve_endpoint(db, reference, name, kind, evidence, glossary):
    """Accept verified exact matches or OWL-verified broader classes in permitted vocabularies."""
    candidates = endpoint_candidates(db, reference, name, evidence, glossary)
    sources = TYPE_SOURCES.get(kind, set())
    aliases = _source_aliases(glossary)
    forms, hints = _exact_lookup_forms(name, kind, aliases)
    keys = {}
    for form in forms:
        if tokens := words(form):
            keys.setdefault(' '.join(tokens), set()).update(_trait_name_keys(form, kind))

    short_unexpanded = not hints and len(name.strip()) <= 3 and name.strip().isupper()
    if not sources or not keys or short_unexpanded:
        return {'iri': None, 'label': None, 'mapping': 'unresolved',
                'reason': 'No verified match in the permitted vocabularies.'}, candidates

    matches, blocked_chem = _namespace_matches_exact(db, sources, keys, kind, evidence, name)
    if kind == 'trait or measurement':
        ref_traits = {iri: term for iri, term in matches.items() if _namespace(iri) in {'TO', 'PATO'}}
        matches = ref_traits or matches
    if len(matches) == 1:
        iri, term = next(iter(matches.items()))
        return {'iri': iri, 'label': term['label'], 'mapping': 'exact',
                'reason': 'Unique complete label, exact synonym, or verified naming variant in a permitted vocabulary.',
                'provenance': {'class_source': term.get('source', '')}}, candidates
    if len(matches) > 1:
        return {'iri': None, 'label': None, 'mapping': 'unresolved',
                'reason': 'Several permitted ontology classes share this name.'}, candidates
    if blocked_chem:
        return {'iri': None, 'label': None, 'mapping': 'unresolved',
                'reason': 'Generic element mention lacks a specified chemical form; atomic CHEBI classes are withheld.'}, candidates

    if kind != 'chemical':
        narrow_matches = _narrow_synonym_broader(db, sources, keys, kind)
        if len(narrow_matches) == 1:
            iri, (term, verified) = next(iter(narrow_matches.items()))
            return {'iri': iri, 'label': term['label'], 'mapping': 'broader',
                    'reason': 'Unique OWL-verified narrower synonym (oboInOwl:hasNarrowSynonym) in a permitted vocabulary.',
                    'provenance': verified}, candidates
        if superclass_match := _superclass_broader(db, sources, keys, kind):
            return superclass_match, candidates

    return {'iri': None, 'label': None, 'mapping': 'unresolved',
            'reason': 'No verified exact or justified broader match in the permitted vocabularies.'}, candidates


def _normalized_predicate_variants(predicate):
    worded = ' '.join(words(predicate))
    variants = {worded}
    for pattern, repl in _GRAMMATICAL_RELATION_REWRITES:
        updated = {pattern.sub(repl, v).strip() for v in variants}
        variants.update(v for v in updated if v)
    return variants


_POSITIVE_PROPERTY_IRIS = frozenset({
    'http://purl.obolibrary.org/obo/RO_0002304',
    'http://purl.obolibrary.org/obo/RO_0002213',
    'http://purl.obolibrary.org/obo/RO_0019001',
})
_NEGATIVE_PROPERTY_IRIS = frozenset({
    'http://purl.obolibrary.org/obo/RO_0002305',
    'http://purl.obolibrary.org/obo/RO_0002212',
    'http://purl.obolibrary.org/obo/RO_0019002',
})


def property_polarity_sign(iri, term=None):
    """Return 'positive' or 'negative' for signed ontology object properties."""
    if iri in _POSITIVE_PROPERTY_IRIS:
        return 'positive'
    if iri in _NEGATIVE_PROPERTY_IRIS:
        return 'negative'
    label = str((term or {}).get('label') or '').casefold()
    if 'positively' in label or 'positive effect' in label:
        return 'positive'
    if 'negatively' in label or 'negative effect' in label:
        return 'negative'
    return None


def _polarity_sign(text):
    pos = bool(_POS_POLARITY_RE.search(text or ''))
    neg = bool(_NEG_POLARITY_RE.search(text or ''))
    if pos and not neg:
        return 'positive'
    if neg and not pos:
        return 'negative'
    return None


def _has_reversed_active_order(clause, subject_span, object_span, predicate=''):
    if not (clause and subject_span and object_span) or _PASSIVE_BY_RE.search(clause):
        return False
    s_hit = re.search(rf'(?<!\w){re.escape(subject_span)}(?!\w)', clause, re.I)
    o_hit = re.search(rf'(?<!\w){re.escape(object_span)}(?!\w)', clause, re.I)
    if not (s_hit and o_hit) or o_hit.end() >= s_hit.start():
        return False
    between = clause[o_hit.end():s_hit.start()]
    if ',' in between or re.search(r'\b(?:of|from|between|in)\s*$', between.strip(), re.I):
        return False
    pred_tokens = {t[:5] for t in words(predicate) if len(t) >= 4}
    between_tokens = {t[:5] for t in words(between) if len(t) >= 4}
    return bool((pred_tokens & between_tokens) or re.search(r'\b(?:cannot|can|could|did|does|do|will|may|might)\b', between, re.I))


def _spans_separated_by_clause_boundary(sentence, subject_span, relation_span, object_span):
    """Detect when a relation cue and an endpoint anchor sit across separate clauses/predicates."""
    if not (sentence and relation_span):
        return None
    r_matches = list(re.finditer(rf'(?<!\w){re.escape(relation_span)}(?!\w)', sentence, re.I))
    if not r_matches:
        return None
    for ep_span in (subject_span, object_span):
        if not ep_span:
            continue
        ep_matches = list(re.finditer(rf'(?<!\w){re.escape(ep_span)}(?!\w)', sentence, re.I))
        if not ep_matches:
            continue
        r_m, ep_m = min(
            ((rm, em) for rm in r_matches for em in ep_matches),
            key=lambda pair: abs(pair[1].start() - pair[0].start()),
        )
        left_end = min(r_m.end(), ep_m.end())
        right_start = max(r_m.start(), ep_m.start())
        if left_end < right_start:
            between = sentence[left_end:right_start]
            if _CLAUSE_PREDICATE_BOUNDARY_RE.search(between):
                return (
                    f"Source relation cue '{relation_span}' and endpoint '{ep_span}' "
                    "are separated by an intervening clause or predicate boundary."
                )
    return None


def _relation_clause_rejection(iri, term, predicate, relation_quote, support, local_clause, subject_span, object_span):
    local_id = iri.rsplit('/', 1)[-1]
    has_quote = bool(str(relation_quote or '').strip())
    quote_or_pred = relation_quote if has_quote else predicate

    if cross_err := _spans_separated_by_clause_boundary(local_clause, subject_span, quote_or_pred, object_span):
        return cross_err

    if iri == _RO_NECESSITY_POSITIVE:
        if not _NECESSITY_RE.search(quote_or_pred):
            defn = term.get('definition', '')
            comm = ' '.join(term.get('comments') or ())
            return (f"{local_id} requires explicit necessity in the source clause "
                    f"(definition: {defn} comments: {comm}).")

    if iri == _RO_CORRELATED_WITH:
        if not (re.search(r'\bcorrelat\w*\b', predicate, re.I)
                and re.search(r'\bcorrelat\w*\b', quote_or_pred, re.I)
                and not _NULL_EFFECT_RE.search(support)):
            return f"{local_id} requires explicit statistical correlation wording in both predicate and source quote."

    if has_quote:
        cue = _PROPERTY_QUOTE_CUES.get(local_id)
        if cue and not cue.search(relation_quote):
            return f"Source relation quote '{relation_quote}' does not establish {term['label']} ({local_id})."

    if local_id in _ACTIVE_VERBAL_PROPERTY_IDS:
        clause = local_clause or quote_or_pred
        if pair := _NOMINALIZATION_PATTERNS.get(local_id):
            nom_re, verb_re = pair
            if nom_re.search(clause) and not verb_re.search(clause):
                return f"Source clause mentions a noun phrase rather than active '{term['label']}' predication."
        if _has_reversed_active_order(clause, subject_span, object_span, predicate):
            return f"Source clause word order does not assert subject {term['label']} object."
    return None


def resolve_relation(predicate, reference, *, assertion_status='affirmed', relation_quote='',
                     evidence_text='', subject_span='', object_span='', local_clause=''):
    if assertion_status != 'affirmed':
        return {'iri': None, 'label': None, 'mapping': 'unresolved',
                'reason': f"Assertion status '{assertion_status}' cannot assert a positive relation property."}
    if _QUALIFIED_RELATION.search(predicate) or _QUALIFIED_RELATION.search(relation_quote or ''):
        return {'iri': None, 'label': None, 'mapping': 'unresolved',
                'reason': 'Hedged, negated, or non-significant predicate stays unresolved.'}

    pred_sign = _polarity_sign(predicate)
    quote_sign = _polarity_sign(relation_quote)
    if pred_sign and quote_sign and pred_sign != quote_sign:
        return {'iri': None, 'label': None, 'mapping': 'unresolved',
                'reason': f"Predicate polarity ({pred_sign}) contradicts source relation quote ({quote_sign})."}

    support = ' '.join(t for t in (relation_quote, evidence_text) if t) or predicate
    names = _normalized_predicate_variants(predicate)
    exact_allowed = []
    for iri in reference.profile['allowed_object_properties']:
        term = reference.terms[iri]
        if 'exact' not in term.get('allowed_mappings', ()):
            continue
        labels = {term['label'], *term.get('exact_synonyms', ())}
        if names & {' '.join(words(label)) for label in labels}:
            exact_allowed.append(term)
    if len(exact_allowed) == 1:
        term = exact_allowed[0]
        if err := _relation_clause_rejection(term['iri'], term, predicate, relation_quote, support,
                                             local_clause, subject_span, object_span):
            return {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': err}
        return {'iri': term['iri'], 'label': term['label'], 'mapping': 'exact',
                'reason': 'The source predicate and clause support an approved property label, exact synonym, or grammatical variant.'}

    if _RO_CORRELATED_WITH in reference.profile['allowed_object_properties'] and _EXPLICIT_CORRELATION_RE.search(predicate):
        term = reference.terms[_RO_CORRELATED_WITH]
        if err := _relation_clause_rejection(_RO_CORRELATED_WITH, term, predicate, relation_quote, support,
                                             local_clause, subject_span, object_span):
            return {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': err}
        core = re.sub(r'^(?:is|are|was|were)\s+', '', ' '.join(words(predicate)))
        mapping = 'exact' if core == 'correlated with' else 'broader'
        return {'iri': _RO_CORRELATED_WITH, 'label': term['label'], 'mapping': mapping,
                'reason': 'Explicit statistical correlation wording supported by the source quote.'}

    if _NEGATIVE_EFFECT_RE.search(predicate) and _RO_NEGATIVE_EFFECT in reference.profile['allowed_object_properties']:
        term = reference.terms[_RO_NEGATIVE_EFFECT]
        if 'broader' in term.get('allowed_mappings', ()) and not _NULL_EFFECT_RE.search(support):
            if err := _relation_clause_rejection(_RO_NEGATIVE_EFFECT, term, predicate, relation_quote, support,
                                                 local_clause, subject_span, object_span):
                return {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': err}
            return {'iri': _RO_NEGATIVE_EFFECT, 'label': term['label'], 'mapping': 'broader',
                    'reason': 'Negative causal effect wording supported by source quote; requires compatible BFO process types.'}

    return {'iri': None, 'label': None, 'mapping': 'unresolved',
            'reason': 'No unique approved property preserves the predicate wording, clause evidence, and qualifications.'}


def _reference_for_bindings(db, reference, bindings):
    terms = {}
    pending = [bindings[role]['iri'] for role in ('subject', 'object') if bindings[role]['iri']]
    visited = set()
    while pending:
        iri = pending.pop()
        if iri in visited:
            continue
        visited.add(iri)
        original = reference.terms.get(iri, {})
        record = db.execute('SELECT info FROM terms WHERE iri=? AND NOT obsolete', (iri,)).fetchone()
        if record:
            term = json.loads(record[0])
            term['parents'] = sorted(set(term['parents']) | set(original.get('parents', ())))
            terms[iri] = term
        pending.extend((terms.get(iri) or original).get('parents', ()))
    return SimpleNamespace(terms=ChainMap(terms, reference.terms), graph=reference.graph)


def _resolve_verbatim_span(sentence, quote):
    span = source_surface_mention(sentence, quote)
    if not span:
        return None
    q_stripped = str(quote or '').strip()
    for suffix in ('.)', ')'):
        if q_stripped.endswith(suffix) and not span.endswith(suffix):
            candidate = span + suffix
            idx = sentence.find(candidate)
            if idx != -1:
                return sentence[idx:idx + len(candidate)]
    return span


def _claim_local_clause(sentence, subject_span, relation_span, object_span):
    parts = [part.strip() for part in _SUBORDINATE_SPLIT_RE.split(sentence) if part.strip()]
    if len(parts) <= 1:
        return sentence
    needed = [relation_span]
    for ep in (subject_span, object_span):
        if ep and source_surface_mention(sentence, ep):
            needed.append(ep)
    for part in parts:
        if all(source_surface_mention(part, item) for item in needed):
            return part
    return sentence


def verify_anchors_and_evidence(proposal, target, visible):
    """Verify target-owned relation anchoring and endpoint traceability without modifying source text."""
    owned = set(target['target_sentence_numbers'])
    claim = {role: proposal[role].strip() for role in ROLES}
    if claim['subject'].casefold() == claim['object'].casefold():
        raise ValueError('The subject and object repeat the same phrase.')

    support_ids = proposal.get('support_sentence_ids')
    if support_ids is None:
        support_ids = proposal.get('evidence_sentence_ids') or []
    if any(type(number) is not int for number in support_ids) or not set(support_ids) <= visible.keys():
        raise SchemaValidationError(f'support_sentence_ids={support_ids!r}: must contain valid visible sentence IDs.')
    if support_ids != sorted(set(support_ids)):
        raise SchemaValidationError(f'support_sentence_ids={support_ids!r}: must be unique and sorted ascending.')

    glossary = target.get('source_abbreviation_definitions', ())
    anchors = {}
    anchor_sids = []
    for role in ('subject', 'relation', 'object'):
        raw_anchor = proposal[f'{role}_anchor']
        sid = raw_anchor['sentence_id']
        quote = raw_anchor['quote'].strip()
        if sid not in visible:
            raise ValueError(f'{role}_anchor references non-visible sentence {sid}.')
        if role == 'relation' and sid not in owned:
            raise SchemaValidationError(
                f'context-only finding: relation_anchor sentence_id={sid} must be a target sentence {sorted(owned)}.'
            )
        resolved_quote = _resolve_verbatim_span(visible[sid], quote)
        if not resolved_quote:
            raise ValueError(f'Unanchored {role}_anchor quote.')

        anchor_sids.append(sid)
        if role in ('subject', 'object'):
            if span := _resolve_verbatim_span(resolved_quote, claim[role]):
                match_kind, endpoint_span = 'literal_anchor', span
            else:
                anchored = endpoint_anchor(claim[role], resolved_quote, [visible[sid]], glossary=glossary)
                if not anchored:
                    raise ValueError(f'Unanchored {role}.')
                ep_span, ep_kind, _ = anchored
                contained = (ep_span == resolved_quote
                             or ep_span.casefold() == resolved_quote.casefold()
                             or bool(source_surface_mention(resolved_quote, ep_span)))
                if not contained and ep_kind != 'document_defined_abbreviation':
                    raise ValueError(f'Unanchored {role}.')
                match_kind, endpoint_span = ep_kind, resolved_quote
            anchors[role] = {'sentence_id': sid, 'quote': quote, 'resolved_span': endpoint_span, 'match_kind': match_kind}
        else:
            anchors[role] = {'sentence_id': sid, 'quote': quote, 'resolved_span': resolved_quote, 'match_kind': 'verbatim_quote'}

    ids = sorted({*anchor_sids, *support_ids})
    check_target_evidence({'claims': [{**proposal, 'evidence_sentence_ids': ids}]}, visible.keys(), owned)
    evidence = [visible[number] for number in ids]
    return claim, anchors, evidence, ids


def predicate_evidence_warnings(claim, assertion_status, relation_span, local_clause, *,
                                subject_span='', object_span=''):
    warnings = []
    rel = claim['relation']
    span_no_not_only = re.sub(r'\bnot\s+only\b', '', relation_span, flags=re.I)
    rel_no_not_only = re.sub(r'\bnot\s+only\b', '', rel, flags=re.I)

    pred_sign = _polarity_sign(rel)
    quote_sign = _polarity_sign(relation_span)
    if pred_sign and quote_sign and pred_sign != quote_sign:
        warnings.append({
            'code': 'predicate_polarity_contradiction',
            'message': f"Predicate '{rel}' ({pred_sign}) contradicts source quote '{relation_span}' ({quote_sign}).",
        })

    if _DIRECTIONAL_UPGRADE_RE.search(rel) and _NEUTRAL_AFFECT_RE.search(relation_span) and not _DIRECTIONAL_EVIDENCE_RE.search(relation_span):
        warnings.append({
            'code': 'predicate_strength_upgrade',
            'message': f"Predicate '{rel}' strengthens neutral relation quote '{relation_span}'.",
        })

    if assertion_status == 'affirmed':
        if (_NULL_EFFECT_RE.search(rel) or _NULL_EFFECT_RE.search(relation_span)
                or _UNCERTAIN_CUE_RE.search(rel) or _UNCERTAIN_CUE_RE.search(relation_span)
                or _NEGATION_CUE_RE.search(rel_no_not_only) or _NEGATION_CUE_RE.search(span_no_not_only)):
            warnings.append({
                'code': 'assertion_status_mismatch',
                'message': f"Assertion status 'affirmed' conflicts with negation, non-significance, or hedging in '{rel}' / '{relation_span}'.",
            })
    elif assertion_status == 'non_significant':
        if not (_SIGNIFICANCE_WORD_RE.search(rel) or _SIGNIFICANCE_WORD_RE.search(local_clause)):
            warnings.append({
                'code': 'assertion_status_mismatch',
                'message': "Assertion status 'non_significant' lacks significance wording in the predicate or cited clause.",
            })
    elif assertion_status == 'negated':
        if not (_NEGATION_CUE_RE.search(rel) or _NEGATION_CUE_RE.search(local_clause)):
            warnings.append({
                'code': 'assertion_status_mismatch',
                'message': "Assertion status 'negated' lacks explicit negation in the predicate or cited clause.",
            })
    elif assertion_status == 'uncertain':
        if not (_UNCERTAIN_CUE_RE.search(rel) or _UNCERTAIN_CUE_RE.search(local_clause)):
            warnings.append({
                'code': 'assertion_status_mismatch',
                'message': "Assertion status 'uncertain' lacks hedging or uncertainty cues in the predicate or cited clause.",
            })

    for role in ('subject', 'object'):
        matched_traits = {m.group(0).casefold() for m in _DISTINCT_COORDINATED_TRAIT_RE.finditer(claim[role])}
        if _BUNDLED_BOTH_RE.search(claim[role]) or len(matched_traits) >= 2:
            warnings.append({
                'code': 'possible_bundled_outcomes',
                'message': f"The {role} '{claim[role]}' bundles coordinated outcomes or distinct traits.",
            })

    s_span = subject_span or claim['subject']
    o_span = object_span or claim['object']
    if _has_reversed_active_order(local_clause, s_span, o_span, rel):
        warnings.append({
            'code': 'possible_agent_role_reversal',
            'message': f"Clear active clause '{local_clause}' places '{o_span}' before the predicate and '{s_span}' after it.",
        })

    if (_COMPARATIVE_THAN_RE.search(rel) and s_span and o_span
            and re.search(rf'(?<!\w){re.escape(o_span)}\s+of\s+(?:the\s+)?{re.escape(s_span)}(?!\w)', local_clause, re.I)):
        warnings.append({
            'code': 'possible_comparative_role_swap',
            'message': f"Clause attributes '{o_span} of {s_span}' differently from subject '{s_span}' :: object '{o_span}'.",
        })

    if cross_err := _spans_separated_by_clause_boundary(local_clause, s_span, relation_span, o_span):
        warnings.append({
            'code': 'cross_clause_relation_mismatch',
            'message': cross_err,
        })
    return warnings


def annotate_endpoint_identities(claim, bindings):
    """Preserve distinct measurement meanings alongside broader/exact ontology class assignments."""
    for role in ('subject', 'object'):
        ep = bindings[role]
        phrase = str(claim.get(role) or '')
        phrase_norm = ' '.join(_normalize_hyphens(phrase).split()).casefold()
        iri = ep.get('iri')
        mapping = ep.get('mapping') or 'unresolved'
        shared_concept = bool(iri and mapping == 'exact')
        ep['distinct_meaning'] = phrase
        ep['shared_concept'] = shared_concept
        ep['concept_key'] = (
            f'exact:{iri}' if shared_concept
            else f"{mapping}:{iri or 'unresolved'}:{stable_hash(phrase_norm, 16)}"
        )


def relation_self_link_status(claim, bindings):
    """Distinguish genuine identical-concept self-links from distinct measurements sharing a broader class."""
    annotate_endpoint_identities(claim, bindings)
    sub, obj = bindings['subject'], bindings['object']
    s_iri, o_iri = sub.get('iri'), obj.get('iri')
    if s_iri and s_iri == o_iri:
        if sub.get('concept_key') == obj.get('concept_key'):
            label = sub.get('label') or s_iri
            return (
                True,
                {'self_link': True},
                f"Hard gate: relation cannot form a self-link between identical concept identities "
                f"('{claim['subject']}' and '{claim['object']}' -> {label}).",
            )
        return (
            False,
            {'self_link': False, 'shared_broader_class_distinct_concepts': True},
            None,
        )
    return False, {'self_link': False}, None


def ground_extracted(proposal, target, visible, reference, db):
    claim, anchors, evidence, ids = verify_anchors_and_evidence(proposal, target, visible)
    owned = set(target['target_sentence_numbers'])
    assertion_status = proposal['assertion_status']

    rel_sid = anchors['relation']['sentence_id']
    rel_sentence = visible[rel_sid]
    s_span = anchors['subject']['resolved_span'] if anchors['subject']['sentence_id'] == rel_sid else claim['subject']
    o_span = anchors['object']['resolved_span'] if anchors['object']['sentence_id'] == rel_sid else claim['object']
    local_clause = _claim_local_clause(rel_sentence, s_span, anchors['relation']['resolved_span'], o_span)

    claim['qualifiers'] = {key: value for key, value in proposal['qualifiers'].items() if value}
    claim['source_evidence_span'] = ' '.join(evidence)
    claim['source_evidence_target_fragments'] = [
        {'sentence_number': number, 'text': visible[number]} for number in ids if number in owned
    ]

    bindings, candidates = {}, {}
    for role in ('subject', 'object'):
        bindings[role], candidates[role] = resolve_endpoint(
            db, reference, claim[role], proposal.get(f'{role}_type'), evidence,
            target.get('source_abbreviation_definitions', ()),
        )
    bindings['relation'] = resolve_relation(
        claim['relation'],
        reference,
        assertion_status=assertion_status,
        relation_quote=anchors['relation']['resolved_span'],
        evidence_text=claim['source_evidence_span'],
        subject_span=s_span,
        object_span=o_span,
        local_clause=local_clause,
    )

    projected = _reference_for_bindings(db, reference, bindings)
    assessment = {'original_claim': claim, 'bindings': bindings}
    clause_fragments = [{'sentence_number': rel_sid, 'text': local_clause}]
    for role in ('subject', 'object'):
        sid = anchors[role]['sentence_id']
        if sid in owned and sid != rel_sid:
            clause_fragments.append({'sentence_number': sid, 'text': visible[sid]})
    warnings = extraction_warnings({**claim, 'source_evidence_target_fragments': clause_fragments})
    pred_warnings = predicate_evidence_warnings(
        claim,
        assertion_status,
        anchors['relation']['resolved_span'],
        local_clause,
        subject_span=s_span,
        object_span=o_span,
    )
    hold_reasons = [*warnings, *pred_warnings]

    mapping_warnings = mapping_meaning_warnings(assessment, projected)
    for issue in mapping_warnings:
        bindings[issue['role']] = {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': issue['message']}

    annotate_endpoint_identities(claim, bindings)

    if bindings['relation']['iri']:
        typing = check_property_types(assessment, projected)
        relation_issue = relation_evidence_issue(claim['relation'], bindings['relation'], projected)
        is_self_link, self_prov, self_err = relation_self_link_status(claim, bindings)
        rel_iri = bindings['relation']['iri']
        if is_self_link:
            bindings['relation'] = {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': self_err}
        elif rel_iri in _DIRECTIONAL_CAUSAL_IRIS:
            both_resolved = bool(bindings['subject']['iri'] and bindings['object']['iri'])
            if not both_resolved or typing['status'] != 'compatible' or relation_issue:
                reason = relation_issue or f"Causal property requires compatible resolved endpoint types ({typing['status']})."
                bindings['relation'] = {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': reason}
            else:
                bindings['relation']['provenance'] = {**(bindings['relation'].get('provenance') or {}), **self_prov}
        elif typing['status'] not in {'compatible', 'not_applicable'} or relation_issue:
            bindings['relation'] = {'iri': None, 'label': None, 'mapping': 'unresolved',
                                    'reason': relation_issue or typing['status']}
        else:
            bindings['relation']['provenance'] = {**(bindings['relation'].get('provenance') or {}), **self_prov}

    page = target['sentences'][rel_sid - 1]['page']
    return {
        **claim,
        'assertion_status': assertion_status,
        'anchors': anchors,
        'local_clause': local_clause,
        'bindings': bindings,
        'endpoint_types': {role: proposal.get(f'{role}_type') for role in candidates},
        'target_id': target['target_id'],
        'source_page': page,
        'evidence_sentence_ids': ids,
        'review': 'held' if hold_reasons else 'not_flagged',
        'extraction_warnings': warnings,
        'hold_reasons': hold_reasons,
        'evidence_decisions': {
            'relation_in_target': True,
            'anchors_verified': True,
            'hold_count': len(hold_reasons),
        },
        'classified': not hold_reasons and all(binding['iri'] for binding in bindings.values()),
    }, candidates


_PCT_RE = re.compile(r'\d+(?:\.\d+)?\s*%')
_COUPLED_COMPARATIVE_RE = re.compile(
    r'\b(?:respectively|(?:less|more|higher|lower|greater|smaller)\s+than|remained|became)\b',
    re.I,
)


def should_use_extraction_thinking(passage):
    """Enable Thinking selectively only for target lines with tightly coupled percentages/comparisons."""
    target_text = ' '.join(
        line.split(' target: ', 1)[1]
        for line in str(passage or '').splitlines()
        if ' target: ' in line
    )
    return len(_PCT_RE.findall(target_text)) >= 2 and bool(_COUPLED_COMPARATIVE_RE.search(target_text))


def run_extract_then_ground(pdf, reference, client, output, progress, budget, lookup_dir, *,
                            targets=None, thinking_client=None):
    targets = source_targets(pdf, with_abbreviations=True) if targets is None else targets
    if not targets:
        raise ValueError('The PDF has no extractable text.')
    system, schema = extraction_request(reference)
    db, signature = open_lookup(lookup_dir)
    save_json(output / 'prompt.json', {'system': system, 'response_schema': schema,
                                      'workflow': POLICY, 'lookup_signature': signature})
    save_rows(output / 'targets.jsonl', targets)
    progress(total_targets=len(targets), workflow=POLICY, lookup_signature=signature)
    rows, rejections, suggestions, seen = [], [], [], set()
    with closing(db):
        for index, target in enumerate(targets, 1):
            check_cancelled()
            visible = local_sentences(target)
            owned = set(target['target_sentence_numbers']) & visible.keys()
            passage = '\n'.join(f"{number} {'target' if number in owned else 'neighbor'}: {line}"
                                for number, line in visible.items())
            if owned:
                active_client = (
                    thinking_client
                    if thinking_client is not None and should_use_extraction_thinking(passage)
                    else client
                )
                answer = active_client.chat_json(system, passage, max_tokens=budget, json_schema=schema,
                                                 request_label=f'typed extraction target {index}/{len(targets)}')
                for proposal in answer['claims']:
                    check_cancelled()
                    try:
                        claim, candidates = ground_extracted(proposal, target, visible, reference, db)
                    except (SchemaValidationError, ValueError) as error:
                        rejections.append({'target_id': target['target_id'], 'proposal': proposal, 'reason': str(error)})
                        continue
                    key = stable_hash(json.dumps(claim, sort_keys=True, ensure_ascii=False), 32)
                    if key not in seen:
                        seen.add(key)
                        rows.append({'id': key, **claim})
                        suggestions.append({'claim_id': key, 'target_id': target['target_id'], 'endpoints': candidates})
            save_rows(output / 'claims.jsonl', rows)
            save_rows(output / 'endpoint_candidates.jsonl', suggestions)
            save_rows(output / 'proposal_rejections.jsonl', rejections)
            progress(completed_targets=index, claim_count=len(rows), rejection_count=len(rejections),
                     classified_count=sum(row['classified'] for row in rows))
