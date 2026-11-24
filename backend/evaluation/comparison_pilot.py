"""v4 Controlled Pilot: MeasuredComparison representation + Trait-Location grounded components."""
from __future__ import annotations

import json
import re
from pathlib import Path

from backend.evaluation.endpoint_verifier import (
    _PECO_LIMITED_PHOSPHATE_IRI,
    _PECO_PHOSPHATE_IRI,
    _PHOSPHATE_FORM_RE,
    _PHOSPHATE_PI_RE,
    POLICY as VERIFIER_POLICY,
    _rejoin_pdf_word_hyphens,
    apply_verified_decisions,
    prepare_endpoint_verification_items,
    verify_passage_endpoints,
)
from backend.evaluation.extract_then_ground import (
    _BUNDLED_BOTH_RE,
    _DISTINCT_COORDINATED_TRAIT_RE,
    _NULL_EFFECT_RE,
)
from backend.evaluation.ontology_lookup import endpoint_candidates
from backend.ontology.catalog import catalog
from backend.shared.text import source_surface_mention

PILOT_POLICY = 'v4-comparison-pilot-v4'
_POTATO_TAXON_IRI = 'http://purl.obolibrary.org/obo/NCBITaxon_4113'
_PATO_CONCENTRATION_IRI = 'http://purl.obolibrary.org/obo/PATO_0000033'
_PATO_RATIO_IRI = 'http://purl.obolibrary.org/obo/PATO_0001470'
_PATO_EFFICIENCY_IRI = 'http://purl.obolibrary.org/obo/PATO_0001029'
_PATO_ACCUMULATION_IRI = 'http://purl.obolibrary.org/obo/PATO_0002269'

_PRECOMPOSED_ORGAN_LOCATIONS = {
    'http://purl.obolibrary.org/obo/TO_0020114': ('shoot', 'http://purl.obolibrary.org/obo/PO_0009006', 'shoot system'),
    'http://purl.obolibrary.org/obo/TO_0001124': ('root', 'http://purl.obolibrary.org/obo/PO_0009005', 'root'),
}

_ORGAN_LOCATION_MAP = {
    'shoot': ('shoot', 'http://purl.obolibrary.org/obo/PO_0009006', 'shoot system', ('shoot', 'aerial', 'aboveground', 'above-ground')),
    'shoots': ('shoot', 'http://purl.obolibrary.org/obo/PO_0009006', 'shoot system', ('shoot', 'aerial', 'aboveground', 'above-ground')),
    'root': ('root', 'http://purl.obolibrary.org/obo/PO_0009005', 'root', ('root', 'radicle')),
    'roots': ('root', 'http://purl.obolibrary.org/obo/PO_0009005', 'root', ('root', 'radicle')),
    'leaf': ('leaf', 'http://purl.obolibrary.org/obo/PO_0025034', 'leaf', ('leaf', 'leaves', 'foliar', 'lamina')),
    'leaves': ('leaf', 'http://purl.obolibrary.org/obo/PO_0025034', 'leaf', ('leaf', 'leaves', 'foliar', 'lamina')),
    'foliar': ('leaf', 'http://purl.obolibrary.org/obo/PO_0025034', 'leaf', ('leaf', 'leaves', 'foliar', 'lamina')),
    'tuber': ('tuber', 'http://purl.obolibrary.org/obo/PO_0004543', 'shoot axis tuber', ('tuber',)),
    'tubers': ('tuber', 'http://purl.obolibrary.org/obo/PO_0004543', 'shoot axis tuber', ('tuber',)),
    'stem': ('stem', 'http://purl.obolibrary.org/obo/PO_0009047', 'stem', ('stem', 'stalk', 'culm')),
    'stems': ('stem', 'http://purl.obolibrary.org/obo/PO_0009047', 'stem', ('stem', 'stalk', 'culm')),
}

_PCT_PAIR_RESPECTIVELY_RE = re.compile(
    r'(\d+(?:\.\d+)?)\s*%\s+and\s+(\d+(?:\.\d+)?)\s*%\s+'
    r'(?:less|lower|higher|more|greater)\s+than\s+(?:that\s+of\s+)?(?:the\s+)?'
    r'([A-Za-z0-9-]+)\s+and\s+(?:the\s+)?([A-Za-z0-9-]+)(?:\s+ones)?,?\s+respec\s*-?\s*tively',
    re.I,
)
_NS_EVIDENCE_RE = re.compile(
    r'\b(?:not\s+(?:statistically\s+)?significant\w*|no\s+significant\w*|did\s+not\s+(?:observe|significantly)|remained\s+constant)\b',
    re.I,
)
_NUMBER_IN_TEXT_RE = re.compile(r'(\d+(?:\.\d+)?)\s*%')
_CONDITION_PREP_PREFIX_RE = re.compile(r'^(?:in|under|at|during|across)\s+', re.I)
_COMPARATIVE_CUE_PREFIX_RE = re.compile(r'\b(?:than|compared\s+(?:to|with)|versus|vs\.?|relative\s+to)\s+$', re.I)
_CONCENTRATION_TERM_RE = re.compile(r'\bconcentrations?\b', re.I)
_CONTENT_TERM_RE = re.compile(r'\bcontents?\b', re.I)
_RATIO_OR_POOLED_RE = re.compile(
    r'\b(?:root[-\s]+to[-\s]+shoot|shoot[-\s]+to[-\s]+root|root\s*/\s*shoot|shoot\s*/\s*root|'
    r'whole[-\s]+plant|total\s+plant|combined\s+shoot|pooled)\b',
    re.I,
)
_ORGAN_WORD_PATTERN = r'(?:roots?|shoots?|leaves|leaf|foliar|tubers?|stems?)'
_ORGAN_MODIFIER_PATTERN = r'(?:old(?:er)?|young(?:er)?|upper|lower|apical|basal|mature|developing)'
_COORDINATED_ORGAN_RE = re.compile(
    rf'\b(?:{_ORGAN_MODIFIER_PATTERN}\s+)?{_ORGAN_WORD_PATTERN}\s+(?:and|or|,)\s+'
    rf'(?:(?:both|the)\s+)?(?:{_ORGAN_MODIFIER_PATTERN}\s+)?{_ORGAN_WORD_PATTERN}\b',
    re.I,
)
_COORDINATED_SUB_ORGAN_RE = re.compile(
    rf'\b{_ORGAN_MODIFIER_PATTERN}\s+(?:and|or)\s+{_ORGAN_MODIFIER_PATTERN}\s+{_ORGAN_WORD_PATTERN}\b',
    re.I,
)
_COORDINATED_CONC_CONTENT_RE = re.compile(
    r'\b(?:concentrations?\s+(?:and|or)\s+contents?|contents?\s+(?:and|or)\s+concentrations?)\b',
    re.I,
)
_ORGAN_MODIFIER_RE = re.compile(rf'\b({_ORGAN_MODIFIER_PATTERN})\s+{_ORGAN_WORD_PATTERN}\b', re.I)
_TEMPORAL_FROM_TO_RE = re.compile(
    r'\bfrom\s+(?:\d+\s*(?:DAT|DAP|days?|weeks?|h|hours?)|day\s+\d+|P\d+(?:\.\d+)?)\s+'
    r'to\s+(?:\d+\s*(?:DAT|DAP|days?|weeks?|h|hours?)|day\s+\d+|P\d+(?:\.\d+)?)\b',
    re.I,
)
_EXPLICIT_COMPARATOR_CUE_RE = re.compile(
    r'\b(?:lower|less|higher|more|greater|smaller|larger)\s+than\s+(?!\d|one\b|two\b|half\b)'
    r'|\bcompared\s+(?:to|with)\b|\bversus\b|\brelative\s+to\b',
    re.I,
)
_CLAUSE_SPLIT_RE = re.compile(
    r'(?<![A-Z])(?<!\bcv)(?<!\bvs)\.(?!\d)\s+|;\s+'
    r'|\b(?:Fur\s*-\s*thermore|Furthermore|Moreover|In\s+contrast|Whereas)\b,?\s*'
    r'|\bas\s+well\s+as\b\s*|\bwhile\b\s*|\bexcept\s+for\b\s*',
    re.I,
)
_CLAUSE_STOPWORDS = frozenset({'of', 'the', 'in', 'at', 'and', 'or', 'under', 'with', 'for', 'by', 'on', 'to', 'a', 'an', 'was', 'were', 'had'})
_UNDIRECTED_EFFECT_RE = re.compile(
    r'\b(?:significantly\s+|sig\s*-\s*nificantly\s+)?(?:affected|influenced|altered|impacted|governed|modulated)(?:\s+by)?\b'
    r'|\bsignificant\s+(?:effect|interaction|variation)\b',
    re.I,
)
_DIRECTIONAL_CHANGE_CUE_RE = re.compile(
    r'\b(?:higher|lower|more|less|greater|smaller|larger|increased?|decreased?|declined?|'
    r'reduced?|reduction|elevated?|elevation|amplified?|surpassed?|exceeded?|peaked?|peaks?|'
    r'highest|lowest|better|worse|superior|inferior|diminished|heightened|rise|drop)\b',
    re.I,
)
_SOLUBLE_SUGAR_EVIDENCE_RE = re.compile(r'\bsoluble\s+sugars?\b', re.I)
_SUGAR_TERM_RE = re.compile(r'\bsugars?\b', re.I)
_IAA_TERM_RE = re.compile(r'\b(?:iaa|indole[-\s]*3[-\s]*acetic\s+acid)\b', re.I)
_ATTENUATED_RESPONSE_RE = re.compile(
    r'\b(?:less|more|weakly|strongly|minimally|partially|slightly)\s+'
    r'(?:influenced|affected|dependent|responsive|pronounced|impacted|sensitive)\b',
    re.I,
)
_ORDERED_RATIO_PAIR_RE = re.compile(
    r'\b(?P<num1>roots?|shoots?|leaves|leaf|foliar|tubers?|stems?)\s*(?:-\s*to\s*-|\s+to\s+|/|:)\s*'
    r'(?P<den1>roots?|shoots?|leaves|leaf|foliar|tubers?|stems?)\s+ratios?\b'
    r'|\bratios?\s+of\s+(?P<num2>roots?|shoots?|leaves|leaf|foliar|tubers?|stems?)\s+to\s+'
    r'(?P<den2>roots?|shoots?|leaves|leaf|foliar|tubers?|stems?)\b',
    re.I,
)
_TABLE_INTERRUPTED_PREFIX_RE = re.compile(
    r'^(?P<prefix>.+?\b(?:was|were|is|are))\s+Table\s+\d+\b.*$',
    re.I,
)
_TABLE_CONTINUATION_RE = re.compile(
    r'^(?:Page\s+\d+\s+of\s+\d+\s+)?'
    r'(?P<continuation>\d+(?:\s*[–−-]\s*\d+)?%\s+(?:higher|lower|more|less|greater|smaller)\s+than\b(?:\.\d+|[^.])+\.)'
    r'(?P<remainder>.*)$',
    re.I,
)


def _bundled_trait_reason(phrase: str | None) -> str | None:
    """Return a hold reason when a trait phrase bundles coordinated organs, sub-organs, or distinct traits."""
    text = ' '.join(str(phrase or '').split())
    if not text:
        return None
    if _COORDINATED_CONC_CONTENT_RE.search(text):
        return f"Bundled trait phrase '{text}' combines concentration and content; split into atomic observations."
    if len({m.group(0).casefold() for m in _DISTINCT_COORDINATED_TRAIT_RE.finditer(text)}) >= 2:
        return f"Bundled trait phrase '{text}' combines multiple distinct traits; split into atomic observations."
    if _BUNDLED_BOTH_RE.search(text):
        return f"Bundled trait phrase '{text}' uses coordinated 'both ... and ...'; split into atomic observations."
    if not _RATIO_OR_POOLED_RE.search(text):
        if _COORDINATED_ORGAN_RE.search(text) or _COORDINATED_SUB_ORGAN_RE.search(text):
            return f"Bundled anatomical phrase '{text}' combines coordinated organs/sub-organs; split before grounding."
    return None


def _local_comparison_clause(row: dict, source_text: str) -> str:
    """Select the local grammatical clause inside a compound sentence that backs this row."""
    text = str(source_text or '').strip()
    pieces = [text]
    m_lead = re.match(r'^(?:while|although|though)\b([^,]{10,}),\s+(.+)$', text, re.I)
    if m_lead:
        pieces = [m_lead.group(1).strip(), m_lead.group(2).strip()]
    clauses = [
        c.strip()
        for piece in pieces
        for c in _CLAUSE_SPLIT_RE.split(piece)
        if c and c.strip()
    ]
    if len(clauses) <= 1:
        return text
    trait = row.get('measured_trait') or {}
    cond = row.get('condition') or {}
    diff = str(row.get('reported_difference') or '')
    num_tokens = re.findall(r'\d+(?:[.,-]\d+)?%?', diff)
    trait_tokens = {
        tok.casefold()
        for tok in re.findall(r'[A-Za-z0-9_;.-]+', str(trait.get('phrase') or ''))
        if len(tok) >= 2 and tok.casefold() not in _CLAUSE_STOPWORDS
    }
    role_tokens = {
        tok.casefold()
        for val in (row.get('study_group'), row.get('comparator_label'), cond.get('phrase'))
        for tok in re.findall(r'[A-Za-z0-9_;.-]+', str(val or ''))
        if len(tok) >= 2 and tok.casefold() not in _CLAUSE_STOPWORDS
    }
    src_quote = ' '.join(str(row.get('source_quote') or '').split()).casefold()

    best_clause = text
    best_score = 0
    for clause in clauses:
        cl_low = clause.casefold()
        score = sum(8 for nt in num_tokens if nt.casefold() in cl_low)
        score += sum(3 for tt in trait_tokens if re.search(rf'\b{re.escape(tt)}\b', cl_low))
        score += sum(2 for rt in role_tokens if re.search(rf'\b{re.escape(rt)}\b', cl_low))
        if src_quote and len(src_quote) < len(text) * 0.85 and (cl_low in src_quote or src_quote in cl_low):
            score += 6
        if score > best_score:
            best_score = score
            best_clause = clause
    return best_clause


def _cand_names_lower(iri: str, label: str | None, db) -> str:
    parts = [label or '']
    rec = db.execute('SELECT info FROM terms WHERE iri=?', (iri,)).fetchone()
    if rec:
        info = json.loads(rec[0])
        parts.append(info.get('label') or '')
        parts.extend(info.get('exact_synonyms') or ())
    return ' '.join(p for p in parts if p).casefold()


def _find_missing_organ_component(phrase: str, iri: str, cand_names_low: str, db):
    if '/PO_' in (iri or ''):
        return None
    tokens = [t.casefold() for t in re.findall(r'[A-Za-z]+', phrase or '')]
    for tok in tokens:
        if tok in _ORGAN_LOCATION_MAP:
            loc_word, loc_iri, fallback_label, allowed_words = _ORGAN_LOCATION_MAP[tok]
            if any(w in cand_names_low for w in allowed_words):
                continue
            rec = db.execute('SELECT obsolete, info FROM terms WHERE iri=?', (loc_iri,)).fetchone()
            if rec and not rec[0]:
                loc_label = json.loads(rec[1]).get('label') or fallback_label
                return loc_word, loc_iri, loc_label
    return None


def _extract_analyte_component(
    phrase: str,
    iri: str | None,
    cand_names_low: str,
    *,
    methods_context: str = '',
    source_text: str = '',
) -> dict | None:
    """Extract an ungrounded analyte component (e.g. 'soluble sugar') when the mapped trait is generic."""
    if iri != _PATO_CONCENTRATION_IRI and 'concentration of' not in cand_names_low:
        return None
    if _SUGAR_TERM_RE.search(phrase or ''):
        evidence = f'{phrase} {source_text} {methods_context}'
        if _SOLUBLE_SUGAR_EVIDENCE_RE.search(evidence):
            in_phrase = 'soluble' in (phrase or '').casefold()
            return {
                'phrase': 'soluble sugar',
                'surface_phrase': 'soluble sugar' if in_phrase else 'sugar',
                'iri': None,
                'label': None,
                'status': 'source_wording',
                'provenance': 'source_sentence' if in_phrase else 'methods_context',
            }
        return {
            'phrase': 'sugar',
            'surface_phrase': 'sugar',
            'iri': None,
            'label': None,
            'status': 'source_wording',
            'provenance': 'source_sentence',
        }
    if _IAA_TERM_RE.search(phrase or ''):
        return {
            'phrase': 'indole-3-acetic acid (IAA)',
            'surface_phrase': 'IAA',
            'iri': None,
            'label': None,
            'status': 'source_wording',
            'provenance': 'source_sentence',
        }
    return None


def _lookup_organ_entry(word: str, db) -> dict | None:
    entry = _ORGAN_LOCATION_MAP.get((word or '').casefold())
    if not entry:
        return None
    loc_word, loc_iri, fallback_label, _ = entry
    rec = db.execute('SELECT obsolete, info FROM terms WHERE iri=?', (loc_iri,)).fetchone()
    if rec and rec[0]:
        return None
    loc_label = (json.loads(rec[1]).get('label') if rec else None) or fallback_label
    return {'word': loc_word, 'iri': loc_iri, 'label': loc_label}


def _extract_ordered_ratio_locations(phrase: str, db) -> tuple[dict | None, dict | None]:
    """Extract ordered numerator (1) and denominator (2) PO locations for ratio trait phrases."""
    norm_phrase = re.sub(r'\b([a-z]+)-to-([a-z]+)\b', r'\1-to-\2', ' '.join(str(phrase or '').split()), flags=re.I)
    m = _ORDERED_RATIO_PAIR_RE.search(norm_phrase)
    if not m:
        return None, None
    num_raw = m.group('num1') or m.group('num2')
    den_raw = m.group('den1') or m.group('den2')
    num_info = _lookup_organ_entry(num_raw, db)
    den_info = _lookup_organ_entry(den_raw, db)
    num_loc = ({'role': 'numerator_location', 'order': 1, 'mapping': 'exact', **num_info} if num_info else None)
    den_loc = ({'role': 'denominator_location', 'order': 2, 'mapping': 'exact', **den_info} if den_info else None)
    return num_loc, den_loc


def _distinct_efficiency_or_process_identity(phrase: str, definitions: tuple | list = ()) -> tuple[str, str]:
    """Assign a distinct trait identity so P efficiency sub-metrics never collapse on generic PATO classes."""
    text = ' '.join(str(phrase or '').split())
    low = text.casefold()
    alias_text = text
    for d in definitions or ():
        abbr = str((d or {}).get('abbreviation') or '').strip()
        exp = str((d or {}).get('expansion') or '').strip()
        if abbr and exp and abbr.casefold() == low:
            alias_text = f'{exp} ({abbr})'
            low = exp.casefold()
            break
    if re.search(r'\bpae\b|\b(?:p|phosphorus)\s+acquisition\s+efficiency\b', low):
        return 'phosphorus_acquisition_efficiency', 'phosphorus acquisition efficiency (PAE)'
    if re.search(r'\bpupe\b|\b(?:p|phosphorus)\s+uptake\s+efficiency\b', low):
        return 'phosphorus_uptake_efficiency', 'phosphorus uptake efficiency (PUpE)'
    if re.search(r'\bpute\b|\b(?:p|phosphorus)\s+utili[sz]ation\s+efficiency\b', low):
        return 'phosphorus_utilization_efficiency', 'phosphorus utilization efficiency (PUtE)'
    if re.search(r'\bpue\b|\b(?:p|phosphorus)\s+use\s+efficiency\b', low):
        return 'phosphorus_use_efficiency', 'phosphorus use efficiency (PUE)'
    if re.search(r'\b(?:p|phosphorus)\s+accumulation\b', low):
        return 'phosphorus_accumulation', 'phosphorus accumulation'
    slug = re.sub(r'[^a-z0-9]+', '_', (alias_text or text).casefold()).strip('_') or 'unspecified_trait'
    return slug, alias_text or text


def ground_trait_location_components(
    phrase: str,
    binding: dict,
    db,
    *,
    methods_context: str = '',
    source_text: str = '',
    definitions: list[dict] | tuple[dict, ...] = (),
) -> dict:
    """Report bundled holds first, then partial ratio/generic traits, pre-composed classes, or components."""
    if bundle_reason := _bundled_trait_reason(phrase):
        return {
            'status': 'held_bundled_organs',
            'summary': bundle_reason,
            'trait': {'iri': None, 'label': None, 'mapping': 'held'},
            'location': None,
            'location_qualifier': None,
            'analyte': None,
            'ratio_components': None,
            'concept_identity': None,
            'grounding_completeness': 'held',
            'grounded_components': [],
            'source_wording_components': [],
            'chemical_form_specified': False,
            'shared_concept': False,
        }
    m_mod = _ORGAN_MODIFIER_RE.search(phrase or '')
    loc_qual = m_mod.group(1).casefold() if m_mod else None
    mapping = (binding or {}).get('mapping') or 'unresolved'
    iri = (binding or {}).get('iri')
    label = (binding or {}).get('label')
    if mapping in {'exact', 'broader'} and iri:
        if iri == _PATO_RATIO_IRI:
            num_loc, den_loc = _extract_ordered_ratio_locations(phrase, db)
            ordered_ok = bool(
                num_loc and den_loc
                and num_loc.get('order') == 1 and den_loc.get('order') == 2
                and num_loc.get('iri') != den_loc.get('iri')
            )
            grounded = [{'role': 'trait', 'iri': iri, 'label': label, 'mapping': 'broader'}]
            if num_loc:
                grounded.append(num_loc)
            if den_loc:
                grounded.append(den_loc)
            ratio_comp = {'numerator': num_loc, 'denominator': den_loc, 'ordered_pair_complete': ordered_ok}
            concept_id = f"ratio({num_loc['iri']}/{den_loc['iri']})" if ordered_ok else None
            summary = (
                f"{label} (partial grounding; ordered ratio numerator={num_loc['label']} [1], denominator={den_loc['label']} [2])"
                if ordered_ok
                else f'{label} (partial grounding; requires both ordered ratio organs)'
            )
            return {
                'status': 'partial_components',
                'summary': summary,
                'trait': {'iri': iri, 'label': label, 'mapping': 'broader'},
                'location': None,
                'location_qualifier': loc_qual,
                'analyte': None,
                'ratio_components': ratio_comp,
                'concept_identity': concept_id,
                'grounding_completeness': 'partial',
                'grounded_components': grounded,
                'source_wording_components': [{'role': 'ratio_phrase', 'phrase': phrase}],
                'chemical_form_specified': False,
                'shared_concept': False,
            }

        generic_quantity = iri in {'http://purl.obolibrary.org/obo/PATO_0000025', 'http://purl.obolibrary.org/obo/PATO_0000161'}
        if generic_quantity and phrase.strip().casefold() != str(label).casefold():
            return {'status': 'partial_broader', 'grounding_completeness': 'partial',
                    'trait': {'iri': iri, 'label': label, 'mapping': 'broader'},
                    'summary': 'General quantity class omits the measured substance or process.',
                    'concept_identity': None, 'shared_concept': False, 'grounded_components': []}
        if iri in {_PATO_EFFICIENCY_IRI, _PATO_ACCUMULATION_IRI}:
            id_key, id_label = _distinct_efficiency_or_process_identity(phrase, definitions=definitions)
            sw_comps = [{'role': 'specific_trait_identity', 'identity': id_key, 'phrase': id_label}]
            if loc_qual:
                sw_comps.append({'role': 'location_qualifier', 'phrase': loc_qual})
            return {
                'status': 'partial_broader',
                'summary': f"{label} (partial broader; distinct trait '{id_label}')",
                'trait': {'iri': iri, 'label': label, 'mapping': 'broader'},
                'location': None,
                'location_qualifier': loc_qual,
                'analyte': None,
                'ratio_components': None,
                'concept_identity': id_key,
                'grounding_completeness': 'partial',
                'grounded_components': [{'role': 'trait', 'iri': iri, 'label': label, 'mapping': 'broader'}],
                'source_wording_components': sw_comps,
                'chemical_form_specified': False,
                'shared_concept': False,
            }

        cand_names_low = _cand_names_lower(iri, label, db)
        analyte_comp = _extract_analyte_component(
            phrase, iri, cand_names_low, methods_context=methods_context, source_text=source_text,
        )
        missing_organ = _find_missing_organ_component(phrase, iri, cand_names_low, db)
        if missing_organ is not None:
            loc_word, loc_iri, loc_label = missing_organ
            loc_note = f'{loc_word} ({loc_qual})' if loc_qual else loc_word
            summary = f'{label} grounded; {loc_note} location grounded'
            if analyte_comp:
                summary += f"; analyte '{analyte_comp['phrase']}' preserved as source wording"
            grounded = [
                {'role': 'quantity' if analyte_comp else 'trait', 'iri': iri, 'label': label, 'mapping': 'broader'},
                {'role': 'location', 'iri': loc_iri, 'label': loc_label, 'mapping': 'exact', **({'qualifier': loc_qual} if loc_qual else {})},
            ]
            source_words = []
            if analyte_comp:
                source_words.append({
                    'role': 'analyte',
                    'phrase': analyte_comp['phrase'],
                    'surface_phrase': analyte_comp['surface_phrase'],
                    'provenance': analyte_comp['provenance'],
                })
            if loc_qual:
                source_words.append({'role': 'location_qualifier', 'phrase': loc_qual, 'target': loc_word})
            return {
                'status': 'grounded_components',
                'summary': summary,
                'trait': {
                    'iri': iri,
                    'label': label,
                    'mapping': 'broader',
                    **({'downgraded_from_exact': True} if mapping == 'exact' else {}),
                },
                'location': {
                    'iri': loc_iri,
                    'label': loc_label,
                    **({'qualifier': loc_qual} if loc_qual else {}),
                },
                'location_qualifier': loc_qual,
                'analyte': analyte_comp,
                'ratio_components': None,
                'concept_identity': None,
                'grounding_completeness': 'complete',
                'grounded_components': grounded,
                'source_wording_components': source_words,
                'chemical_form_specified': False,
                'shared_concept': False,
            }

        pre_loc = None
        if iri in _PRECOMPOSED_ORGAN_LOCATIONS:
            _, pre_iri, pre_fallback = _PRECOMPOSED_ORGAN_LOCATIONS[iri]
            rec = db.execute('SELECT obsolete, info FROM terms WHERE iri=?', (pre_iri,)).fetchone()
            if not rec or not rec[0]:
                pre_label = (json.loads(rec[1]).get('label') if rec else None) or pre_fallback
                pre_loc = {'iri': pre_iri, 'label': pre_label, **({'qualifier': loc_qual} if loc_qual else {})}

        source_words = []
        if analyte_comp:
            source_words.append({
                'role': 'analyte',
                'phrase': analyte_comp['phrase'],
                'surface_phrase': analyte_comp['surface_phrase'],
                'provenance': analyte_comp['provenance'],
            })
        if loc_qual:
            source_words.append({'role': 'location_qualifier', 'phrase': loc_qual})

        grounded_list = [{'role': 'quantity' if analyte_comp else 'trait', 'iri': iri, 'label': label, 'mapping': 'broader'}]
        if pre_loc:
            grounded_list.append({'role': 'location', 'iri': pre_loc['iri'], 'label': pre_loc['label'], 'mapping': 'exact', **({'qualifier': loc_qual} if loc_qual else {})})

        if mapping == 'exact' and (analyte_comp is not None or (loc_qual and loc_qual not in cand_names_low)):
            reason_parts = []
            if analyte_comp:
                reason_parts.append(f"analyte '{analyte_comp['phrase']}' preserved as source wording")
            if loc_qual and loc_qual not in cand_names_low:
                reason_parts.append(f'omits {loc_qual}')
            return {
                'status': 'named_broader',
                'summary': f"{label} (broader; {'; '.join(reason_parts)})",
                'trait': {'iri': iri, 'label': label, 'mapping': 'broader', 'downgraded_from_exact': True},
                'location': pre_loc,
                'location_qualifier': loc_qual,
                'analyte': analyte_comp,
                'ratio_components': None,
                'concept_identity': None,
                'grounding_completeness': 'complete',
                'grounded_components': grounded_list,
                'source_wording_components': source_words,
                'chemical_form_specified': False,
                'shared_concept': False,
            }
        if mapping == 'exact':
            exact_grounded = [{'role': 'trait', 'iri': iri, 'label': label, 'mapping': 'exact'}]
            if pre_loc:
                exact_grounded.append({'role': 'location', 'iri': pre_loc['iri'], 'label': pre_loc['label'], 'mapping': 'exact'})
            return {
                'status': 'named_exact',
                'summary': f'{label} (exact named class)',
                'trait': {'iri': iri, 'label': label, 'mapping': 'exact'},
                'location': pre_loc,
                'location_qualifier': loc_qual,
                'analyte': None,
                'ratio_components': None,
                'concept_identity': iri,
                'grounding_completeness': 'complete',
                'grounded_components': exact_grounded,
                'source_wording_components': source_words,
                'chemical_form_specified': False,
                'shared_concept': True,
            }
        summary = f'{label} (broader)'
        if analyte_comp:
            summary += f"; analyte '{analyte_comp['phrase']}' preserved as source wording"
        return {
            'status': 'named_broader',
            'summary': summary,
            'trait': {'iri': iri, 'label': label, 'mapping': 'broader'},
            'location': pre_loc,
            'location_qualifier': loc_qual,
            'analyte': analyte_comp,
            'ratio_components': None,
            'concept_identity': None,
            'grounding_completeness': 'complete',
            'grounded_components': grounded_list,
            'source_wording_components': source_words,
            'chemical_form_specified': False,
            'shared_concept': False,
        }
    return {
        'status': 'unresolved',
        'summary': (binding or {}).get('reason') or 'unresolved',
        'trait': {'iri': None, 'label': None, 'mapping': 'unresolved'},
        'location': None,
        'location_qualifier': loc_qual,
        'analyte': None,
        'ratio_components': None,
        'concept_identity': None,
        'grounding_completeness': 'unresolved',
        'grounded_components': [],
        'source_wording_components': [{'role': 'location_qualifier', 'phrase': loc_qual}] if loc_qual else [],
        'chemical_form_specified': False,
        'shared_concept': False,
    }


def _strip_role_text(val: str | None) -> str:
    s = re.sub(r'^[\s\'"“”’]+|[\s\'"“”’.,;]+$', '', str(val or '')).casefold()
    s = re.sub(r'^(?:in|under|at|during|across)\s+(?:the\s+)?', '', s)
    return ' '.join(s.split())


def _surface_or_rejoined(source_text: str, quote: str) -> bool:
    return bool(
        quote and (
            source_surface_mention(source_text, quote)
            or source_surface_mention(_rejoin_pdf_word_hyphens(source_text), quote)
        )
    )


def _sentence_matches_source(source_text: str, sent: str) -> bool:
    s_norm = ' '.join(_rejoin_pdf_word_hyphens(sent or '').split()).casefold()
    t_norm = ' '.join(_rejoin_pdf_word_hyphens(source_text or '').split()).casefold()
    if not s_norm or not t_norm:
        return False
    return s_norm in t_norm or _surface_or_rejoined(source_text, sent)


def assess_comparison_row(row: dict, *, source_text: str, methods_context: str = '') -> dict:
    """Assess completeness, role integrity, and scientific meaning holds across ALL emitted rows."""
    issues: list[str] = []
    hold_reasons: list[str] = []
    if row.get('comparison_axis') not in {'group', 'condition'}:
        issues.append("comparison_axis must be 'group' or 'condition'.")
    trait = row.get('measured_trait') or {}
    trait_phrase = str(trait.get('phrase') or '')
    trait_label = str(trait.get('label') or '')

    if trait.get('component_status') == 'held_bundled_organs' or _bundled_trait_reason(trait_phrase):
        hold_reasons.append('possible_bundled_outcomes')
        issues.append(_bundled_trait_reason(trait_phrase) or 'Held bundled coordinated organs/traits before grounding.')
    elif not trait.get('label') or not trait.get('iri'):
        issues.append('measured_trait must carry both label and grounded ontology IRI.')

    trait_iri = trait.get('iri')
    trait_status = trait.get('component_status')
    is_partial = False
    if trait_iri == _PATO_RATIO_IRI or trait_status == 'partial_components':
        is_partial = True
        rc = trait.get('ratio_components') or {}
        gcs = trait.get('grounded_components') or []
        has_num = bool((rc.get('numerator') or {}).get('iri')) or any(
            g.get('role') == 'numerator_location' and g.get('order') == 1 for g in gcs
        )
        has_den = bool((rc.get('denominator') or {}).get('iri')) or any(
            g.get('role') == 'denominator_location' and g.get('order') == 2 for g in gcs
        )
        if not (has_num and has_den):
            issues.append(
                'Partial ratio grounding: generic PATO:0001470 (ratio) lacks both ordered ratio terms '
                '(e.g., ratio + single organ only) and cannot form a ratio identity.'
            )
        else:
            issues.append(
                'Partial grounding: ratio trait uses generic PATO:0001470 with ordered numerator/denominator '
                'components rather than a dedicated pre-composed ratio trait class.'
            )
    elif trait_iri in {_PATO_EFFICIENCY_IRI, _PATO_ACCUMULATION_IRI} or trait_status == 'partial_broader' or trait.get('grounding_completeness') == 'partial':
        is_partial = True
        issues.append(
            'Partial grounding: generic broader quality class retains broader classification and distinct '
            'trait identity without counting as fully complete.'
        )

    if trait_phrase and trait_label:
        if (_CONCENTRATION_TERM_RE.search(trait_phrase) and not _CONTENT_TERM_RE.search(trait_phrase)
                and _CONTENT_TERM_RE.search(trait_label) and not _CONCENTRATION_TERM_RE.search(trait_label)):
            hold_reasons.append('concentration_content_shift')
            issues.append(f"Quantity mismatch: concentration trait '{trait_phrase}' mapped to content class '{trait_label}'.")
        elif (_CONTENT_TERM_RE.search(trait_phrase) and not _CONCENTRATION_TERM_RE.search(trait_phrase)
              and _CONCENTRATION_TERM_RE.search(trait_label) and not _CONTENT_TERM_RE.search(trait_label)):
            hold_reasons.append('concentration_content_shift')
            issues.append(f"Quantity mismatch: content trait '{trait_phrase}' mapped to concentration class '{trait_label}'.")

    study_group = str(row.get('study_group') or '').strip()
    if not study_group:
        issues.append('Missing study_group.')

    comp_label = str(row.get('comparator_label') or '').strip()
    comp_quote = str(row.get('comparator_quote') or '').strip()
    baseline_status = row.get('baseline_status')
    if baseline_status is None:
        baseline_status = 'explicit' if (comp_label or comp_quote) else 'unspecified'
    if baseline_status not in {'explicit', 'unspecified'}:
        issues.append(f'Invalid baseline_status: {baseline_status!r}.')

    cond = row.get('condition') or {}
    cond_phrase = str(cond.get('phrase') or '').strip()

    if baseline_status == 'explicit':
        if not comp_label:
            issues.append('Missing comparator_label for explicit comparison baseline.')
        if not comp_quote or not _surface_or_rejoined(source_text, comp_quote):
            issues.append('comparator_quote must appear verbatim in the source text.')
        else:
            if cond_phrase and _strip_role_text(comp_label) == _strip_role_text(cond_phrase):
                issues.append('Role error: comparator_label duplicates condition rather than naming an explicit comparison baseline.')
            if _CONDITION_PREP_PREFIX_RE.match(comp_quote):
                idx = source_text.casefold().find(comp_quote.casefold())
                prefix = source_text[max(0, idx - 24):idx] if idx >= 0 else ''
                if not _COMPARATIVE_CUE_PREFIX_RE.search(prefix):
                    issues.append('Role error: comparator_quote is a condition phrase, not an explicit comparison baseline.')
            if comp_label and _strip_role_text(comp_quote) == _strip_role_text(study_group) and _strip_role_text(study_group) != _strip_role_text(comp_label):
                issues.append('Role error: comparator_quote copies study_group instead of the comparison baseline.')
    elif baseline_status == 'unspecified':
        if comp_label and comp_label.casefold() != 'unspecified':
            issues.append('Unspecified comparison baseline must not carry a comparator_label.')
        if comp_quote:
            issues.append('Unspecified comparison baseline must not carry a comparator_quote.')

    direction = row.get('direction')
    if direction not in {'lower', 'higher', 'no_significant_difference', 'unchanged'}:
        issues.append(f'Invalid comparison direction: {direction!r}.')
    diff_text = str(row.get('reported_difference') or '').strip()
    if not diff_text:
        issues.append('Missing reported_difference.')
    sent = str(row.get('source_sentence') or '').strip()
    if not sent or not _sentence_matches_source(source_text, sent):
        issues.append('Missing verifiable source_sentence.')
    src_quote = str(row.get('source_quote') or '').strip()
    if src_quote and not _surface_or_rejoined(source_text, src_quote):
        issues.append('source_quote must appear verbatim in the source text.')

    cond_iri = cond.get('iri')
    if cond_iri in {_PECO_LIMITED_PHOSPHATE_IRI, _PECO_PHOSPHATE_IRI}:
        evidence_pool = f'{source_text} {methods_context}'.strip()
        if not (_PHOSPHATE_FORM_RE.search(evidence_pool) or _PHOSPHATE_PI_RE.search(evidence_pool)):
            issues.append('PECO phosphate exposure requires explicit document-specific Methods phosphate evidence.')

    local_clause = _local_comparison_clause(row, source_text)
    sig = row.get('statistical_significance')
    if direction in {'unchanged', 'no_significant_difference'} and (
        (_ATTENUATED_RESPONSE_RE.search(diff_text) or _ATTENUATED_RESPONSE_RE.search(local_clause))
        and not (_NS_EVIDENCE_RE.search(local_clause) or _NULL_EFFECT_RE.search(local_clause))
    ):
        hold_reasons.append('attenuated_treatment_response_as_unchanged')
        issues.append(
            f"Direction hold: attenuated treatment response ({diff_text!r}) describes response strength, "
            'not unchanged growth or trait level.'
        )
    if direction == 'no_significant_difference':
        has_ns_clause = bool(
            _NS_EVIDENCE_RE.search(local_clause)
            or _NULL_EFFECT_RE.search(local_clause)
            or _NS_EVIDENCE_RE.search(diff_text)
            or _NULL_EFFECT_RE.search(diff_text)
        )
        if sig == 'significant' or not has_ns_clause:
            hold_reasons.append('contradictory_significance_and_direction')
            issues.append(
                'Contradictory significance: direction is no_significant_difference '
                'while statistical_significance is significant or local clause lacks non-significant wording.'
            )
    if direction in {'lower', 'higher'} and (
        _NULL_EFFECT_RE.search(diff_text)
        or _NULL_EFFECT_RE.search(local_clause)
        or _NS_EVIDENCE_RE.search(local_clause)
        or sig == 'not_significant'
    ):
        hold_reasons.append('lost_non_significance')
        issues.append('Directional difference contradicts non-significant phrasing in the local clause.')

    if baseline_status == 'unspecified' and (
        _TEMPORAL_FROM_TO_RE.search(local_clause)
        or _TEMPORAL_FROM_TO_RE.search(source_text)
        or _EXPLICIT_COMPARATOR_CUE_RE.search(local_clause)
    ):
        hold_reasons.append('missed_explicit_baseline')
        issues.append('Baseline marked unspecified despite explicit comparator/timepoint progression in the local clause.')

    m_pair = _PCT_PAIR_RESPECTIVELY_RE.search(source_text)
    if m_pair and baseline_status == 'explicit':
        v1, v2 = f'{m_pair.group(1)}%', f'{m_pair.group(2)}%'
        g1, g2 = m_pair.group(3).casefold(), m_pair.group(4).casefold()
        comp_low = comp_label.casefold()
        diff_pcts = {f'{n}%' for n in _NUMBER_IN_TEXT_RE.findall(diff_text)}
        if g1 in comp_low and g2 not in comp_low and v2 in diff_pcts and v1 not in diff_pcts:
            issues.append(f'Paired mismatch: comparator {g1!r} paired with second value {v2!r} instead of {v1!r}.')
        if g2 in comp_low and g1 not in comp_low and v1 in diff_pcts and v2 not in diff_pcts:
            issues.append(f'Paired mismatch: comparator {g2!r} paired with first value {v1!r} instead of {v2!r}.')

    dedup_holds = list(dict.fromkeys(hold_reasons))
    is_complete = len(issues) == 0 and not dedup_holds
    return {
        'complete': is_complete,
        'complete_explicit_baseline': bool(is_complete and baseline_status == 'explicit'),
        'complete_unspecified_baseline': bool(is_complete and baseline_status == 'unspecified'),
        'partial_grounded': bool(is_partial and not dedup_holds),
        'review': 'held' if dedup_holds else 'not_flagged',
        'hold_reasons': dedup_holds,
        'validation_issues': issues,
        'local_clause': local_clause,
    }


def is_undirected_overall_effect(row: dict, source_text: str = '') -> bool:
    """True when an observation reports a significant overall effect without direction or pairwise comparator."""
    if row.get('baseline_status') == 'explicit' or str(row.get('comparator_label') or '').strip():
        return False
    if row.get('direction') == 'overall_effect':
        return True
    sent = source_text or str(row.get('source_sentence') or '')
    local_clause = _local_comparison_clause(row, sent)
    diff_text = str(row.get('reported_difference') or '')
    return bool(
        (_UNDIRECTED_EFFECT_RE.search(diff_text) or _UNDIRECTED_EFFECT_RE.search(local_clause))
        and not _DIRECTIONAL_CHANGE_CUE_RE.search(diff_text)
        and not _NS_EVIDENCE_RE.search(local_clause)
        and not _NULL_EFFECT_RE.search(local_clause)
    )


def partition_comparisons_and_overall_effects(rows: list[dict], *, methods_context: str = '') -> tuple[list[dict], list[dict]]:
    """Assess rows first so holds are recorded, then route undirected overall effects outside directional comparisons."""
    directional: list[dict] = []
    routed: list[dict] = []
    for row in rows:
        item = dict(row)
        sent = str(item.get('source_sentence') or '')
        assessed = assess_comparison_row(item, source_text=sent, methods_context=methods_context)
        item['complete'] = assessed['complete']
        item['complete_explicit_baseline'] = assessed['complete_explicit_baseline']
        item['complete_unspecified_baseline'] = assessed['complete_unspecified_baseline']
        item['partial_grounded'] = assessed['partial_grounded']
        item['review'] = assessed['review']
        item['hold_reasons'] = assessed['hold_reasons']
        item['validation_issues'] = assessed['validation_issues']
        if is_undirected_overall_effect(item, sent):
            routed.append({
                'id': item.get('id'),
                'target_id': item.get('target_id'),
                'source_page': item.get('source_page'),
                'record_type': 'undirected_overall_effect',
                'route_reason': 'significant_overall_effect_without_direction_or_pairwise_comparator',
                'measured_trait': item.get('measured_trait'),
                'study_group': item.get('study_group'),
                'factor_or_condition': item.get('condition'),
                'statistical_significance': item.get('statistical_significance'),
                'reported_effect': item.get('reported_difference'),
                'source_sentence': sent,
                'source_quote': item.get('source_quote'),
                'raw_direction': item.get('direction'),
                'raw_review': item.get('review'),
                'raw_hold_reasons': item.get('hold_reasons'),
            })
        else:
            directional.append(item)
    return directional, routed


def validate_comparison_row(row: dict, *, source_text: str, methods_context: str = '') -> tuple[bool, list[str]]:
    """Enforce complete slot grounding, comparison roles, meaning holds, full-sentence anchors, and paired ordering."""
    assessed = assess_comparison_row(row, source_text=source_text, methods_context=methods_context)
    return assessed['complete'], assessed['validation_issues']


def _ground_term_deterministic_or_candidate(phrase: str, kind: str, evidence: list[str], definitions: list[dict], db, reference):
    """Ground a resolved component using deterministic rules first, fallback to exact/label-matched lookup candidate."""
    from backend.evaluation.extract_then_ground import resolve_endpoint

    binding, cands = resolve_endpoint(db, reference, phrase, kind, evidence, definitions)
    if binding.get('iri'):
        return binding
    # Narrow fallback for verified single-class trait/exposure phrase split out of 'trait of study_group'
    p_low = ' '.join(re.sub(r'\s+-\s+', '', phrase).casefold().split())
    for c in cands:
        c_low = (c.get('label') or '').casefold()
        if c_low == p_low or (p_low in {'low phosphorus', 'low p', 'lp', 'lp conditions', 'low-p conditions'} and c['iri'] == _PECO_LIMITED_PHOSPHATE_IRI):
            return {
                'iri': c['iri'],
                'label': c['label'],
                'mapping': 'exact' if c_low == p_low else 'broader',
                'reason': f"Resolved split component '{phrase}' via local candidate '{c['label']}'.",
                'provenance': {'class_source': c.get('class_source', '')},
            }
    return binding


def project_saved_comparisons_zero_llm(claims: list[dict], *, methods_context: str, db, reference, targets_by_id: dict | None = None) -> list[dict]:
    """Deterministically reconstruct MeasuredComparison rows from saved v3 comparative claims (0 LLM calls)."""
    rows: list[dict] = []
    for c in claims:
        rel = str(c.get('relation') or '').strip()
        m_dir = re.search(r'\b(less|lower|higher|more|greater)\s+than\b', rel, re.I)
        if not m_dir:
            continue
        subj = str(c.get('subject') or '').strip()
        obj = str(c.get('object') or '').strip()
        # Match 'Trait of Group' vs anaphoric 'that of Comparator'
        m_split = re.match(r'^(.+?)\s+of\s+(?:the\s+)?(.+)$', subj, re.I)
        m_anaph = re.match(r'^that\s+of\s+(?:the\s+)?(.+)$', obj, re.I)
        if not (m_split and m_anaph):
            continue

        trait_phrase = m_split.group(1).strip()
        study_group = m_split.group(2).strip()
        comp_head = m_anaph.group(1).strip()
        head_noun_m = re.search(r'\b(rooting\s+type|genotypes?|cultivars?|group|lines?)\b$', study_group, re.I)
        if comp_head.casefold().endswith('ones') and head_noun_m:
            comparator_label = re.sub(r'\s+ones$', f' {head_noun_m.group(1)}', comp_head, flags=re.I)
        elif head_noun_m and head_noun_m.group(1).casefold() not in comp_head.casefold():
            comparator_label = f'{comp_head} {head_noun_m.group(1)}'
        else:
            comparator_label = comp_head

        tgt = (targets_by_id or {}).get(c.get('target_id', ''), {})
        defs = tgt.get('source_abbreviation_definitions', [])
        ev_text = c.get('source_evidence_span', '')
        qual = c.get('qualifiers') or {}

        trait_bind = _ground_term_deterministic_or_candidate(trait_phrase, 'trait or measurement', [ev_text], defs, db, reference)
        comp_trait = ground_trait_location_components(trait_phrase, trait_bind, db)

        cond_raw = str(qual.get('condition') or '').strip()
        cond_norm = re.sub(r'^(?:in|at|under)\s+', '', cond_raw, flags=re.I).strip()
        if cond_norm.upper() == 'LP':
            cond_phrase = 'low phosphorus'
        else:
            cond_phrase = cond_norm or cond_raw

        cond_bind = {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': 'No condition specified.'}
        if cond_phrase:
            raw_cond = _ground_term_deterministic_or_candidate(cond_phrase, 'condition or exposure', [ev_text, methods_context], defs, db, reference)
            # Re-verify through exact production hard gate using Methods evidence!
            if raw_cond.get('iri') in {_PECO_LIMITED_PHOSPHATE_IRI, _PECO_PHOSPHATE_IRI}:
                pool = f'{ev_text} {methods_context}'.strip()
                if not (_PHOSPHATE_FORM_RE.search(pool) or _PHOSPHATE_PI_RE.search(pool)):
                    raw_cond = {'iri': None, 'label': None, 'mapping': 'unresolved',
                                'reason': 'Hard gate: PECO phosphate exposure requires explicit source/methods phosphate evidence.'}
            cond_bind = raw_cond

        word = m_dir.group(1).casefold()
        direction = 'lower' if word in {'less', 'lower'} else 'higher'
        metric = str(qual.get('metric') or '').strip()
        reported_diff = f'{metric} {direction}'.strip() if 'average' in metric.casefold() else f'{metric} {direction}'.strip()
        # Format e.g. 'on average 19.5%' -> '19.5% lower on average'
        m_avg = re.match(r'^on\s+average\s+(.+)$', metric, re.I)
        if m_avg:
            reported_diff = f'{m_avg.group(1).strip()} {direction} on average'

        comp_anchor = ((c.get('anchors') or {}).get('object') or {}).get('quote') or obj
        row = {
            'source_claim_id': c.get('id'),
            'comparison_axis': 'group',
            'measured_trait': {
                'phrase': trait_phrase,
                **comp_trait['trait'],
                'component_status': comp_trait['status'],
                'component_summary': comp_trait['summary'],
                'location': comp_trait['location'],
                'location_qualifier': comp_trait.get('location_qualifier'),
                'analyte': comp_trait.get('analyte'),
                'grounded_components': comp_trait.get('grounded_components', []),
                'source_wording_components': comp_trait.get('source_wording_components', []),
                'chemical_form_specified': comp_trait['chemical_form_specified'],
                'shared_concept': comp_trait['shared_concept'],
            },
            'study_group': study_group,
            'baseline_status': 'explicit',
            'comparator_label': comparator_label,
            'comparator_quote': comp_anchor,
            'condition': {
                'phrase': cond_phrase or cond_raw,
                'iri': cond_bind.get('iri'),
                'label': cond_bind.get('label'),
                'mapping': cond_bind.get('mapping'),
                'reason': cond_bind.get('reason'),
            },
            'direction': direction,
            'reported_difference': reported_diff,
            'crop': {'phrase': qual.get('crop_or_species') or 'potato', 'iri': _POTATO_TAXON_IRI, 'label': 'Solanum tuberosum'},
            'source_sentence': ev_text,
            'source_page': c.get('source_page'),
        }
        assessed = assess_comparison_row(row, source_text=ev_text, methods_context=methods_context)
        row['complete'] = assessed['complete']
        row['complete_explicit_baseline'] = assessed['complete_explicit_baseline']
        row['complete_unspecified_baseline'] = assessed['complete_unspecified_baseline']
        row['review'] = assessed['review']
        row['hold_reasons'] = assessed['hold_reasons']
        row['validation_issues'] = assessed['validation_issues']
        rows.append(row)
    return rows


COMPARISON_SCHEMA = {
    'type': 'object',
    'additionalProperties': False,
    'required': ['comparisons'],
    'properties': {
        'comparisons': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': [
                    'comparison_axis', 'measured_trait', 'trait_kind', 'study_group', 'baseline_status',
                    'comparator_label', 'comparator_quote', 'direction', 'statistical_significance',
                    'reported_difference', 'condition', 'cultivar', 'crop', 'sentence_id', 'source_quote',
                ],
                'properties': {
                    'comparison_axis': {'type': 'string', 'enum': ['group', 'condition']},
                    'measured_trait': {'type': 'string', 'minLength': 1},
                    'trait_kind': {'type': 'string', 'enum': ['trait or measurement', 'biological process']},
                    'study_group': {'type': 'string', 'minLength': 1},
                    'baseline_status': {'type': 'string', 'enum': ['explicit', 'unspecified']},
                    'comparator_label': {'type': ['string', 'null']},
                    'comparator_quote': {'type': ['string', 'null']},
                    'direction': {'type': 'string', 'enum': ['lower', 'higher', 'no_significant_difference', 'unchanged']},
                    'statistical_significance': {'type': 'string', 'enum': ['significant', 'not_significant', 'not_reported']},
                    'reported_difference': {'type': 'string', 'minLength': 1},
                    'condition': {'type': ['string', 'null']},
                    'cultivar': {'type': ['string', 'null']},
                    'crop': {'type': ['string', 'null']},
                    'sentence_id': {'type': 'integer', 'minimum': 1},
                    'source_quote': {'type': 'string', 'minLength': 1},
                },
            },
        },
    },
}

COMPARISON_PROMPT = """Extract explicit quantitative or qualitative comparisons in potato–phosphorus studies into atomic structured rows.
Rules:
- Extract ONLY empirical comparisons supported in `target` sentences: check EVERY numbered `target` sentence so earlier target sentences are not skipped when later target sentences emit multiple rows. Capture clear empirical qualitative, dose-trend, and null findings in target sentences (such as "an increased RSA resulted in better P-uptake in potato", "an increase was observed in P concentration ... with an increment in P level", "no significant difference ... among the cultivars", or "At LP, NeLD increased"). When a compound target sentence contrasts an empirical potato finding from `the present study` (even inside an introductory `Although it has been shown that ... in both the present study and ...` clause) against a literature finding in another crop (such as wheat), extract ONLY the potato finding from `the present study` and exclude the non-potato crop clause, hedged speculation ("may stimulate"/"could reduce"), and literature statements rejected by "does not match with our data".
- Atomic splitting (strict): If a sentence coordinates multiple organs, sub-organs, or distinct traits (such as "P concentration in old leaves and roots and P content in shoots and roots" or "sugar concentration and content"), emit ONE separate row per trait × organ pair (e.g. "P concentration in old leaves", "P concentration in roots", "P content in shoots", "P content in roots"). Preserve anatomical qualifiers like "old" or "young" in `measured_trait` (e.g. "P concentration in old leaves"). Never split single ratio/whole-plant traits like "root-to-shoot ratio".
- Coordinated cultivar-specific conditions (strict): When a comparative sentence reports an effect for multiple cultivars under different phosphorus conditions (such as "The sugar content in the roots of Lady Claire plants inoculated with B. subtilis was 20–33% higher than that in non-inoculated plants across both P levels and in Milva at P0.5"), emit ONE separate row per cultivar, preserving the measured trait ("sugar content in the roots"), the comparator ("non-inoculated plants"), the reported difference ("20–33% higher"), and each cultivar's specific phosphorus condition ("across both P levels" for Lady Claire; "at P0.5" for Milva).
- Separate the standalone `measured_trait` (e.g., "P uptake", "TRL", "shoot phosphorus contents", "IAA concentration") from the `study_group`, `condition`, and `comparator_label`.
- Keep `condition` and `comparator_label` strictly distinct: NEVER put the treatment condition (such as "LP treatment" or "at P30") in `comparator_label` or `comparator_quote`.
- Sentence-local comparator discipline (strict): `comparator_quote` MUST appear verbatim inside the SAME numbered `sentence_id` target sentence — NEVER borrow a cultivar name or phrase such as "than that in Milva" from an adjacent `neighbor` or next sentence. If the `sentence_id` target sentence itself does not name a baseline verbatim (for example, "However, at P30, Lady Claire exhibited a higher P concentration in old leaves and roots and an increased P content in shoots and roots."), you MUST set `baseline_status="unspecified"`, `comparator_label=null`, and `comparator_quote=null`.
- Temporal progressions ("from T1 to T2", such as "from 25 DAT to 42 DAT"): set `comparison_axis="condition"`, `study_group` naming the later stage (with population/group if stated), `baseline_status="explicit"`, `comparator_label` naming T1 (e.g. "25 DAT"), and `comparator_quote` copying the verbatim baseline phrase.
- Set `baseline_status="explicit"` ONLY when the `sentence_id` target sentence itself explicitly names a comparator group, dose, or earlier timepoint, copying the verbatim comparator substring from that same sentence into `comparator_quote`.
- When a `target` sentence reports a higher, lower, constant, or changed level under a `condition` WITHOUT naming a comparator group/dose/earlier timepoint inside that sentence, set `baseline_status="unspecified"`, `comparator_label=null`, and `comparator_quote=null`.
- Use `comparison_axis="group"` when comparing cultivars/rooting types/treatments against each other, and `"condition"` when comparing doses, timepoints, or environments.
- Pair "respectively" values in strict textual order so each group keeps its exact numeric value or range in `reported_difference`.
- Preserve `direction="no_significant_difference"` and `statistical_significance="not_significant"` ONLY when the clause actually states non-significance (such as "no significant difference" or "not statistically significant", even alongside a `%` value), and `"unchanged"` strictly for traits that remained constant / did not change. NEVER use `direction="unchanged"` for differential or attenuated treatment responsiveness (such as "growth less influenced by B. subtilis" or "less pronounced"); omit attenuated-responsiveness strength statements or do not encode them as unchanged.
- Exclude undirected overall ANOVA / factor-effect summaries that state only that factors "significantly affected" a trait without giving a direction (`higher`/`lower`) or pairwise comparator (such as "PAE, PUpE, PUtE, and PUE were significantly affected by P levels") — NEVER emit an ANOVA factor effect with `direction="no_significant_difference"`.
- If a target passage contains no valid empirical comparison (for instance, only category transition percentages, undirected ANOVA factor summaries, or contradicted/hedged speculation), return {"comparisons": []}.

Abstract example:
1 target: Trait X of Type S was on average 14% and 21% lower than that of Type I and Type L, respectively, under Treatment Z.
Output:
{"comparisons":[
  {"comparison_axis":"group","measured_trait":"Trait X","trait_kind":"trait or measurement","study_group":"Type S","baseline_status":"explicit","comparator_label":"Type I","comparator_quote":"that of Type I","direction":"lower","statistical_significance":"significant","reported_difference":"14% lower on average","condition":"Treatment Z","cultivar":null,"crop":"potato","sentence_id":1,"source_quote":"Trait X of Type S was on average 14% and 21% lower than that of Type I and Type L, respectively, under Treatment Z."},
  {"comparison_axis":"group","measured_trait":"Trait X","trait_kind":"trait or measurement","study_group":"Type S","baseline_status":"explicit","comparator_label":"Type L","comparator_quote":"that of Type I and Type L","direction":"lower","statistical_significance":"significant","reported_difference":"21% lower on average","condition":"Treatment Z","cultivar":null,"crop":"potato","sentence_id":1,"source_quote":"Trait X of Type S was on average 14% and 21% lower than that of Type I and Type L, respectively, under Treatment Z."}
]}"""


def verify_and_stitch_table_interrupted_targets(
    targets: list[dict],
    pdf_path: Path | None = None,
) -> tuple[list[dict], list[dict]]:
    """Repair body prose split across intervening tables/page breaks after verifying both PDF pages."""
    from copy import deepcopy

    repaired = deepcopy(targets)
    pdf_page_norms: dict[int, str] = {}
    if pdf_path is not None and Path(pdf_path).is_file():
        from pypdf import PdfReader

        reader = PdfReader(str(pdf_path))
        for page_idx, page_obj in enumerate(reader.pages, 1):
            pdf_page_norms[page_idx] = ' '.join((page_obj.extract_text() or '').split())

    stitches: list[dict] = []
    for tgt in repaired:
        sents = tgt.get('sentences') or ()
        owned_set = set(tgt.get('target_sentence_numbers') or ())
        for sid in sorted(owned_set):
            if not (1 <= sid <= len(sents)):
                continue
            sent_obj = sents[sid - 1]
            raw_text = ' '.join(str(sent_obj.get('text') or '').split())
            m_pref = _TABLE_INTERRUPTED_PREFIX_RE.match(raw_text)
            if not m_pref:
                continue
            prefix = m_pref.group('prefix').strip()
            p_start = int(sent_obj.get('page') or tgt.get('target_page_start') or 0)
            if not prefix or not p_start:
                continue

            # Search forward in this target's full sentence table for the interrupted continuation fragment.
            for cont_idx in range(sid, len(sents)):
                cont_obj = sents[cont_idx]
                p_end = int(cont_obj.get('page') or p_start)
                if p_end <= p_start or (p_end - p_start) > 3:
                    continue
                cont_raw = ' '.join(str(cont_obj.get('text') or '').split())
                m_cont = _TABLE_CONTINUATION_RE.match(cont_raw)
                if not m_cont:
                    continue
                continuation = ' '.join(m_cont.group('continuation').split()).strip()
                remainder = ' '.join(m_cont.group('remainder').split()).strip()

                pdf_verified = True
                if pdf_page_norms:
                    pref_norm = ' '.join(prefix.split())
                    cont_norm = ' '.join(continuation.split())
                    pdf_verified = bool(
                        pref_norm in pdf_page_norms.get(p_start, '')
                        and cont_norm in pdf_page_norms.get(p_end, '')
                    )
                if not pdf_verified:
                    continue

                stitched = f'{prefix} {continuation}'.strip()
                prov = {
                    'prefix_page': p_start,
                    'prefix_sentence_number': sid,
                    'prefix_quote': prefix,
                    'continuation_page': p_end,
                    'continuation_sentence_number': cont_idx + 1,
                    'continuation_quote': continuation,
                    'stitched_sentence': stitched,
                    'pdf_verified': True,
                }
                orig_cont_text = cont_obj.get('text')
                for t_upd in repaired:
                    t_sents = t_upd.get('sentences') or ()
                    for s_upd in t_sents:
                        s_norm = ' '.join(str(s_upd.get('text') or '').split())
                        if s_norm == raw_text and int(s_upd.get('page') or 0) == p_start:
                            s_upd['text'] = stitched
                            s_upd['source_pages'] = [p_start, p_end]
                            s_upd['page_span'] = f'{p_start}-{p_end}'
                            s_upd['interrupted_sentence_provenance'] = prov
                        elif orig_cont_text and s_norm == cont_raw and int(s_upd.get('page') or 0) == p_end:
                            s_upd['text'] = remainder
                    if t_upd['target_id'] == tgt['target_id']:
                        t_upd['target_page_start'] = p_start
                        t_upd['target_page_end'] = max(int(t_upd.get('target_page_end') or p_end), p_end)
                        t_upd['source_pages'] = [p_start, p_end]
                        t_upd['page_span'] = f'{p_start}-{p_end}'
                        t_upd['interrupted_sentence_provenance'] = prov
                stitches.append({'target_id': tgt['target_id'], **prov})
                break
    return repaired, stitches


def extract_and_ground_comparisons(
    targets: list[dict],
    *,
    methods_context: str,
    client,
    verifier_client,
    db,
    reference_24,
    lookup_dir: Path,
    candidate_fn=None,
    routed_overall_effects: list[dict] | None = None,
    cache_dir: Path | None = None,
) -> list[dict]:
    """Run MeasuredComparison extraction + retriever/verifier grounding + Trait/Quantity-Location-Analyte components."""
    from backend.evaluation.single_stage import local_sentences

    extracted_rows = []
    synthetic_claims = []
    synthetic_cands = {}
    targets_by_id = {}
    if cache_dir is not None:
        cache_dir.mkdir(parents=True, exist_ok=True)

    for tgt in targets:
        targets_by_id[tgt['target_id']] = tgt
        visible = local_sentences(tgt)
        owned = set(tgt['target_sentence_numbers']) & visible.keys()
        passage = '\n'.join(
            f"{n} {'target' if n in owned else 'neighbor'}: {_rejoin_pdf_word_hyphens(line)}"
            for n, line in visible.items()
        )
        cache_file = (cache_dir / f"ext_{tgt['target_id']}.json") if cache_dir is not None else None
        if cache_file is not None and cache_file.exists():
            ans = json.loads(cache_file.read_text(encoding='utf-8'))
        else:
            ans = client.chat_json(COMPARISON_PROMPT, passage, max_tokens=2048, json_schema=COMPARISON_SCHEMA,
                                   request_label=f"comparison pilot {tgt['target_id'][-8:]}")
            if cache_file is not None:
                cache_file.write_text(json.dumps(ans, ensure_ascii=False, indent=2), encoding='utf-8')
        for idx, item in enumerate(ans.get('comparisons') or []):
            sid = item['sentence_id']
            if sid not in owned:
                continue
            sent_text = visible[sid]
            sent_meta = tgt['sentences'][sid - 1] if sid - 1 < len(tgt['sentences']) else {}
            row_id = f"{tgt['target_id']}:{idx}"
            cond_text = str(item.get('condition') or '').strip()
            defs = tgt.get('source_abbreviation_definitions', ())
            comp_label = item.get('comparator_label') or ''
            bundle_reason = _bundled_trait_reason(item['measured_trait'])
            is_ns = item['direction'] == 'no_significant_difference' or item.get('statistical_significance') == 'not_significant'
            proposal = {
                'subject': item['measured_trait'],
                'subject_type': item['trait_kind'],
                'relation': 'compared_with',
                'object': cond_text or comp_label or item['study_group'],
                'object_type': 'condition or exposure' if cond_text else 'organism',
                'assertion_status': 'non_significant' if is_ns else 'affirmed',
                'qualifiers': {
                    'crop_or_species': item.get('crop'),
                    'population': item.get('cultivar') or item['study_group'],
                    'condition': cond_text or None,
                    'comparator': comp_label or None,
                    'metric': item['reported_difference'],
                },
                'support_sentence_ids': [],
                'subject_anchor': {'sentence_id': sid, 'quote': item['measured_trait'] if _surface_or_rejoined(sent_text, item['measured_trait']) else item['source_quote']},
                'relation_anchor': {'sentence_id': sid, 'quote': item['source_quote']},
                'object_anchor': {'sentence_id': sid, 'quote': cond_text if (cond_text and _surface_or_rejoined(sent_text, cond_text)) else item['source_quote']},
            }
            if candidate_fn is not None:
                cands_trait = [] if bundle_reason else candidate_fn(item['measured_trait'], item['trait_kind'], [sent_text, methods_context], defs)
                cands_cond = candidate_fn(cond_text, 'condition or exposure', [sent_text, methods_context], defs) if cond_text else []
            else:
                cands_trait = [] if bundle_reason else endpoint_candidates(db, reference_24, item['measured_trait'], [sent_text, methods_context], defs)
                cands_cond = endpoint_candidates(db, reference_24, cond_text, [sent_text, methods_context], defs) if cond_text else []
            src_pages = sent_meta.get('source_pages') or tgt.get('source_pages')
            claim = {
                **{k: proposal[k] for k in ('subject', 'relation', 'object', 'assertion_status', 'qualifiers')},
                'id': row_id,
                'target_id': tgt['target_id'],
                'source_page': sent_meta.get('page') if sent_meta else tgt.get('target_page_start'),
                **({'source_pages': src_pages, 'page_span': sent_meta.get('page_span') or tgt.get('page_span')} if src_pages else {}),
                **({'interrupted_sentence_provenance': sent_meta.get('interrupted_sentence_provenance') or tgt.get('interrupted_sentence_provenance')}
                   if (sent_meta.get('interrupted_sentence_provenance') or tgt.get('interrupted_sentence_provenance')) else {}),
                'source_evidence_span': sent_text,
                'local_clause': sent_text,
                'anchors': {
                    'subject': {'sentence_id': sid, 'quote': proposal['subject_anchor']['quote'], 'resolved_span': proposal['subject_anchor']['quote']},
                    'relation': {'sentence_id': sid, 'quote': item['source_quote'], 'resolved_span': item['source_quote']},
                    'object': {'sentence_id': sid, 'quote': proposal['object_anchor']['quote'], 'resolved_span': proposal['object_anchor']['quote']},
                },
                'endpoint_types': {'subject': item['trait_kind'], 'object': 'condition or exposure' if cond_text else 'organism'},
                'bindings': {
                    'subject': (
                        {'iri': None, 'label': None, 'mapping': 'held', 'reason': bundle_reason}
                        if bundle_reason else _ground_term_deterministic_or_candidate(
                            item['measured_trait'], item['trait_kind'], [sent_text], defs, db, reference_24,
                        )
                    ),
                    'relation': {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': 'Comparison frame.'},
                    'object': ({'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': 'Unchecked.'} if cond_text else {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': 'Comparator only.'}),
                },
                'review': 'not_flagged',
                'hold_reasons': [],
                'classified': False,
                '_raw_comp': item,
                '_sent_text': sent_text,
                '_definitions': defs,
            }
            synthetic_claims.append(claim)
            synthetic_cands[row_id] = {'subject': cands_trait, 'object': cands_cond}

    if synthetic_claims:
        for t in targets_by_id.values():
            t.setdefault('_attached_methods', methods_context)
        items = prepare_endpoint_verification_items(synthetic_claims, synthetic_cands, db, reference_24, targets_by_id=targets_by_id)
        for it in items:
            if methods_context and not it.get('methods_context'):
                it['methods_context'] = methods_context
        decs = verify_passage_endpoints(items, verifier_client) if items else {}
        grounded_claims = apply_verified_decisions(synthetic_claims, decs, {it['item_id']: it for it in items}, db, reference_24)
    else:
        grounded_claims = []

    for c in grounded_claims:
        raw = c['_raw_comp']
        sent_text = c['_sent_text']
        comp_trait = ground_trait_location_components(
            raw['measured_trait'],
            c['bindings']['subject'],
            db,
            methods_context=methods_context,
            source_text=sent_text,
            definitions=c.get('_definitions', ()),
        )
        cond_bind = c['bindings']['object'] if raw.get('condition') else {'iri': None, 'label': None, 'mapping': 'not_applicable', 'reason': None}
        crop_str = (raw.get('crop') or '').casefold()
        is_potato = (not crop_str) or ('potato' in crop_str) or ('solanum' in crop_str) or bool(raw.get('cultivar'))
        row = {
            'id': c['id'],
            'target_id': c['target_id'],
            'source_page': c['source_page'],
            **({'source_pages': c['source_pages'], 'page_span': c['page_span']} if c.get('source_pages') else {}),
            **({'interrupted_sentence_provenance': c['interrupted_sentence_provenance']} if c.get('interrupted_sentence_provenance') else {}),
            'comparison_axis': raw['comparison_axis'],
            'measured_trait': {
                'phrase': raw['measured_trait'],
                **comp_trait['trait'],
                'component_status': comp_trait['status'],
                'component_summary': comp_trait['summary'],
                'grounding_completeness': comp_trait.get('grounding_completeness', 'complete'),
                'location': comp_trait['location'],
                'location_qualifier': comp_trait.get('location_qualifier'),
                'analyte': comp_trait.get('analyte'),
                'ratio_components': comp_trait.get('ratio_components'),
                'concept_identity': comp_trait.get('concept_identity'),
                'grounded_components': comp_trait.get('grounded_components', []),
                'source_wording_components': comp_trait.get('source_wording_components', []),
                'chemical_form_specified': comp_trait['chemical_form_specified'],
                'shared_concept': comp_trait['shared_concept'],
            },
            'study_group': raw['study_group'],
            'baseline_status': raw['baseline_status'],
            'comparator_label': raw.get('comparator_label'),
            'comparator_quote': raw.get('comparator_quote'),
            'direction': raw['direction'],
            'statistical_significance': raw.get('statistical_significance', 'not_reported'),
            'reported_difference': raw['reported_difference'],
            'condition': {
                'phrase': raw.get('condition'),
                'iri': cond_bind.get('iri'),
                'label': cond_bind.get('label'),
                'mapping': cond_bind.get('mapping'),
                'reason': cond_bind.get('reason'),
            },
            'cultivar': raw.get('cultivar'),
            'crop': {'phrase': raw.get('crop') or 'potato', 'iri': _POTATO_TAXON_IRI if is_potato else None, 'label': 'Solanum tuberosum' if is_potato else raw.get('crop')},
            'source_sentence': sent_text,
            'source_quote': raw.get('source_quote'),
        }
        assessed = assess_comparison_row(row, source_text=sent_text, methods_context=methods_context)
        row['complete'] = assessed['complete']
        row['complete_explicit_baseline'] = assessed['complete_explicit_baseline']
        row['complete_unspecified_baseline'] = assessed['complete_unspecified_baseline']
        row['partial_grounded'] = assessed['partial_grounded']
        row['review'] = assessed['review']
        row['hold_reasons'] = assessed['hold_reasons']
        row['validation_issues'] = assessed['validation_issues']
        extracted_rows.append(row)

    directional_rows, routed_rows = partition_comparisons_and_overall_effects(
        extracted_rows, methods_context=methods_context,
    )
    if routed_overall_effects is not None:
        routed_overall_effects.extend(routed_rows)
    return directional_rows
