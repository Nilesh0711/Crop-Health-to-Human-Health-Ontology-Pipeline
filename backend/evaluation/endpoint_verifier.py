"""Bounded Phase B endpoint candidate verifier decoupled from extraction and relation rules."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from contextlib import closing
from copy import deepcopy
from pathlib import Path

from backend.domain.extraction_review import mapping_meaning_warnings
from backend.evaluation.extract_then_ground import (
    _COMPARATIVE_THAN_RE,
    _CULTIVAR_OR_GENOTYPE_RE,
    _DIRECTIONAL_CAUSAL_IRIS,
    _DISTINCT_COORDINATED_TRAIT_RE,
    _NULL_EFFECT_RE,
    _PASSIVE_BY_RE,
    _QUALIFIED_RELATION,
    TYPE_SOURCES,
    _db_directory,
    _exact_lookup_forms,
    _has_reversed_active_order,
    _is_blocked_elemental_chemical,
    _namespace,
    _normalize_hyphens,
    _polarity_sign,
    _reference_for_bindings,
    annotate_endpoint_identities,
    property_polarity_sign,
    relation_self_link_status,
    resolve_relation,
)
from backend.evaluation.ontology_lookup import _expand, _source_aliases, open_lookup, verify_narrow_synonym_in_owl, words
from backend.evaluation.single_stage import relation_evidence_issue
from backend.ontology.catalog import Catalog
from backend.ontology.property_types import check_property_types, verifiable_upper_types
from backend.ontology.usage import direct_property_allowed
from backend.shared.artifacts import save_rows
from backend.shared.storage import read_jsonl
from backend.shared.text import source_surface_mention
from backend.stages.ontology_grounding.contract import GROUNDING_INSTRUCTIONS
from backend.stages.ontology_grounding.vocabulary import catalog_term_map


POLICY = 'bounded-endpoint-verifier-v2'

WHITELISTED_TYPE_CHALLENGES = {
    'trait or measurement': ('biological process', 'assay variable'),
    'biological process': ('trait or measurement',),
    'assay variable': ('trait or measurement',),
    'organism': ('food',),
    'food': ('organism',),
}

_PECO_SUPPLEMENT_IRI = 'http://purl.obolibrary.org/obo/PECO_0007397'
_PECO_LIMITED_PHOSPHATE_IRI = 'http://purl.obolibrary.org/obo/PECO_0001046'
_PECO_PHOSPHATE_IRI = 'http://purl.obolibrary.org/obo/PECO_0001006'
_TO_SINGLE_ROOT_LENGTH_IRI = 'http://purl.obolibrary.org/obo/TO_0000227'
_RO_CAUSALLY_INFLUENCES_IRI = 'http://purl.obolibrary.org/obo/RO_0002566'
_RO_CAUSALLY_INFLUENCED_BY_IRI = 'http://purl.obolibrary.org/obo/RO_0002559'
_RO_0019001_IRI = 'http://purl.obolibrary.org/obo/RO_0019001'
_RO_REGULATES_CHAR_IRIS = frozenset({
    'http://purl.obolibrary.org/obo/RO_0019000',
    _RO_0019001_IRI,
    'http://purl.obolibrary.org/obo/RO_0019002',
})
_FORWARD_ACTIVE_CAUSAL_IRIS = frozenset({
    _RO_CAUSALLY_INFLUENCES_IRI,
    'http://purl.obolibrary.org/obo/RO_0002211',
    'http://purl.obolibrary.org/obo/RO_0002212',
    'http://purl.obolibrary.org/obo/RO_0002213',
    'http://purl.obolibrary.org/obo/RO_0002304',
    'http://purl.obolibrary.org/obo/RO_0002305',
    'http://purl.obolibrary.org/obo/RO_0002411',
    'http://purl.obolibrary.org/obo/RO_0019000',
    _RO_0019001_IRI,
    'http://purl.obolibrary.org/obo/RO_0019002',
})
_EFFICIENCY_ENDPOINT_RE = re.compile(
    r'\b(?:pupe|pue|phosphorus\s+uptake\s+efficiency|nutrient\s+use\s+efficiency|'
    r'use\s+efficiency|uptake\s+efficiency|acquisition\s+efficiency)\b',
    re.I,
)
_EFFICIENCY_CLASS_RE = re.compile(r'\befficiency\b', re.I)
_NON_SCALAR_MORPHOLOGY_RE = re.compile(
    r'\b(?:root\s+)?(?:morphology|architecture|structure|development|growth|formation|system)\b',
    re.I,
)
_SCALAR_DIMENSION_RE = re.compile(
    r'\b(?:length|area|volume|diameter|density|biomass|weight|mass|content|concentration|'
    r'uptake|efficiency|rate|count|number|index|ratio|yield|height|depth|intensity|magnitude)\b',
    re.I,
)
_OBSERVATION_CONDITION_REL_RE = re.compile(
    r'\b(?:decreased|increased|reduced|declined|lower|higher|smaller|greater|altered|varied|remained(?:\s+constant)?)\s+'
    r'(?:under|at|in|beyond|during)\b',
    re.I,
)
_RELATION_MODIFIER_LOSS_RE = re.compile(
    r'\b(?:significant(?:ly)?|positive(?:ly)?|negative(?:ly)?|strong(?:ly)?|weak(?:ly)?|modest(?:ly)?|linear(?:ly)?|overall|partial(?:ly)?)\b',
    re.I,
)
_NEUTRAL_INFLUENCE_VERB_RE = re.compile(r'\b(?:affect(?:s|ed|ing)?|influenc(?:e|es|ed|ing))\b', re.I)
_TO_AVERAGE_WEIGHT_IRIS = frozenset({
    'http://purl.obolibrary.org/obo/TO_0000078',
    'http://purl.obolibrary.org/obo/TO_0000578',
})
_COLLECTIVE_PO_PARENTS = frozenset({
    'http://purl.obolibrary.org/obo/PO_0025007',
    'http://purl.obolibrary.org/obo/PO_0025497',
})
_GENERIC_UPPER_BLOCK_IDS = frozenset({'ENVO_01000254', 'RO_0002577', 'PECO_0001001'})

_MEASUREMENT_ONLY_RE = re.compile(
    r'^(?:the\s+majority\s+of\s+)?[<>≤≥~±]?\d+(?:\.\d+)?(?:\s*[–−-]\s*\d+(?:\.\d+)?)?\s*%(?:\s+of\s+(?:the\s+)?\w+)?$',
    re.I,
)
_DEFICIENCY_ONLY_RE = re.compile(r'\b(?:deficien\w*|scarc\w*|shortage|starv\w*|deplet\w*|lack(?:ing)?|absen\w*)\b', re.I)
_EXPOSURE_ARM_WORDING_RE = re.compile(
    r'\b(?:exposure|exposures|treatment|treatments|condition|conditions|level|levels|regime|regimes|'
    r'supply|solution|media|medium|lp|p0\.5|p2|p5|p30)\b',
    re.I,
)
_LOW_DOSE_P_RE = re.compile(
    r'\b(?:low[-\s]+(?:p|phosphorus|phosphate)|lp|limit(?:ed|ing)\s+(?:p|phosphorus|phosphate)|p0\.5|p2)\b',
    re.I,
)
_EXPERIMENTAL_P_ARM_RE = re.compile(
    r'\b(?:lp|hp|p0\.5|p2|p5|p30|(?:low|high)[-\s]+(?:p|phosphorus|phosphate)\s*(?:conditions?|treatments?|levels?|environments?)?)\b',
    re.I,
)
_P_ADDITION_EVIDENCE_RE = re.compile(
    r'\b(?:supplement\w*|added|adding|addition|applied|applying|supplied|supply|fertiliz\w*|nutrient\s+solution|'
    r'hoagland|kh\s*2\s*po\s*4|nah\s*2\s*po\s*4|ca\(h\s*2\s*po\s*4\)2|h\s*2\s*po\s*4|hpo\s*4|superphosphate)\b',
    re.I,
)
_PHOSPHATE_FORM_RE = re.compile(
    r'\b(?:phosphate\w*|orthophosphate\w*|kh\s*2\s*po\s*4|nah\s*2\s*po\s*4|ca\(h\s*2\s*po\s*4\)2|h\s*2\s*po\s*4|hpo\s*4|po\s*4|superphosphate)\b',
    re.I,
)
_PHOSPHATE_PI_RE = re.compile(r'\bPi\b')
_LIMITED_EXPOSURE_RE = re.compile(
    r'\b(?:low|lp|p0\.5|p2|limit(?:ed|ing)|deficien\w*|scarc\w*|shortage|deplet\w*|starv\w*|absen\w*|without|zero)\b',
    re.I,
)
_COLLECTIVE_STRUCTURE_RE = re.compile(
    r'\b(?:network|networks|system|systems|architecture|architectures|canopy|canopies|foliage|crown|crowns)\b',
    re.I,
)
_SYSTEM_CUMULATIVE_ROOT_LENGTH_RE = re.compile(
    r'\b(?:(?:total|cumulative|summed|overall|entire|whole|network)\s+root\s+length|root\s+system\s+length|network\s+length)\b',
    re.I,
)
_COUNT_SUBSET_RE = re.compile(r'\b(?:only\s+)?(?:\d+|one|two|three|four|five)\s+out\s+of\s+\d+\b', re.I)
_MULTI_ARM_CONTRAST_RE = re.compile(
    r'\b(?:hp|high[-\s]+phosphorus)\b.{0,20}\band\b.{0,20}\b(?:lp|low[-\s]+phosphorus)\b'
    r'|\b(?:lp|low[-\s]+phosphorus)\b.{0,20}\band\b.{0,20}\b(?:hp|high[-\s]+phosphorus)\b'
    r'|\bp0\.5\b.{0,25}\band\b.{0,25}\bp(?:5|30)\b',
    re.I,
)
_AGGREGATION_MODIFIER_RE = re.compile(
    r'\b(?:total|average|mean|median|maximum|minimum|cumulative|net|specific|relative)\b',
    re.I,
)
_AVERAGING_CUE_RE = re.compile(r'\b(?:average[ds]?|mean|means)\b', re.I)
_DESCRIPTIVE_MODIFIER_RE = re.compile(
    r'\b(?:shallow|deep|sparse(?:ly)?|dense(?:ly)?|branched|unbranched|thin|thick|extended|'
    r'longer|shorter|small(?:er)?|large(?:r)?|higher|lower|high|low|lp|hp|'
    r'better|worse|superior|inferior|enhanced|improved|reduced|increased|decreased|greater|less|poor(?:er)?|'
    r'insufficient|excessive|moderate|elevated|restricted|optimal|adequate|general|inoculation)\b',
    re.I,
)
_CONCENTRATION_TERM_RE = re.compile(r'\bconcentrations?\b', re.I)
_CONTENT_TERM_RE = re.compile(r'\bcontents?\b', re.I)
_DRY_MATTER_RE = re.compile(r'(?i:\bdry\s+matter\b)|\bDM\b')
_DRY_MATTER_CONTENT_RE = re.compile(r'\bdry\s+matter\s+content\b', re.I)
_PATO_CONCENTRATION_IRI = 'http://purl.obolibrary.org/obo/PATO_0000033'
_PATO_AGE_QUALITY_IRIS = frozenset({
    'http://purl.obolibrary.org/obo/PATO_0000308',  # old
    'http://purl.obolibrary.org/obo/PATO_0000309',  # young
    'http://purl.obolibrary.org/obo/PATO_0001501',  # immature
    'http://purl.obolibrary.org/obo/PATO_0001701',  # mature
})
_SPECIFIC_ANALYTE_WORDS = (
    'phosphorus', 'phosphate', 'nitrogen', 'nitrate', 'ammonium', 'potassium',
    'sodium', 'calcium', 'magnesium', 'iron', 'zinc', 'copper', 'manganese',
    'cobalt', 'starch', 'sucrose', 'glucose', 'fructose', 'abscisic acid',
    'indole-3-acetic acid', 'auxin',
)
_FREE_SUGAR_RE = re.compile(r'\bfree\s+sugars?\b', re.I)
_PATO_RELATIVE_SIZE_IRIS = frozenset({
    'http://purl.obolibrary.org/obo/PATO_0000586',
    'http://purl.obolibrary.org/obo/PATO_0000587',
})
_QUOTED_STUDY_CATEGORY_RE = re.compile(r'[“"\']\s*(?:small|medium|large)\s*[”"\']', re.I)
_STUDY_CATEGORY_LABEL_RE = re.compile(
    r'^(?:small|large)(?:\s+(?:in|at|under)\s+(?:lp|hp)|(?:\s+rsa\w*))?$',
    re.I,
)
_STUDY_CATEGORY_CONTEXT_RE = re.compile(
    r'\b(?:categoriz\w*|catego\s*-\s*rized|category|classification|remained|considered\s+as|'
    r'developed\s+[“"\']?large|tanglegram|cluster\s+analysis)\b',
    re.I,
)


def _rejoin_pdf_word_hyphens(text):
    return re.sub(r'\b([A-Za-z]{2,})\s+-\s+([A-Za-z]{2,})\b', r'\1\2', str(text or ''))
_ORGANISM_RESTRICT_RE = re.compile(r'\bof\s+(?:the\s+)?(?:potato|plants?|genotypes?|cultivars?)\b', re.I)
_PLANT_ORGAN_LOCATIONS = (
    ('shoot', re.compile(r'\bshoots?\b', re.I), ('shoot', 'aerial', 'aboveground', 'above-ground')),
    ('root', re.compile(r'\broots?\b', re.I), ('root', 'radicle')),
    ('leaf', re.compile(r'\b(?:leaf|leaves|foliar)\b', re.I), ('leaf', 'leaves', 'foliar', 'lamina')),
    ('tuber', re.compile(r'\btubers?\b', re.I), ('tuber',)),
    ('stem', re.compile(r'\bstems?\b', re.I), ('stem', 'stalk', 'culm')),
)

_METHODS_DEF_RE = re.compile(
    r'\b(?:kh\s*2\s*po\s*4|nah\s*2\s*po\s*4|h\s*2\s*po\s*4|hpo\s*4|orthophosphate|superphosphate|'
    r'nutrient\s+solution|hoagland|adjusted\s+(?:to|with)|supplied\s+(?:as|with)|labelled\s+as|labeled\s+as)\b',
    re.I,
)
_METHODS_CODE_RE = re.compile(
    r'(?:\((?:HP|LP)\)|\b(?:HP|LP|P0\.5|P2|P5|P30|low[-\s]+P|high[-\s]+P)\b)',
    re.I,
)
_METHODS_ASSAY_DEF_RE = re.compile(
    r'\b(?:soluble\s+sugar\s+concentration|sugar\s+concentration|iaa\s+concentration|phosphorus\s+concentration)'
    r'\b.{0,140}\b(?:determined|measured|analyzed|analysed|quantified|according\s+to)\b',
    re.I,
)


def extract_treatment_methods_context(targets, max_sentences=3):
    """Find exact PDF Methods sentences (with page numbers) defining P treatments and analyte assays."""
    if not targets:
        return '', []
    seen = set()
    candidates = []
    # Targets often share the document's full sentence list; inspect unique sentences in order.
    for target in targets:
        for idx, sent in enumerate(target.get('sentences') or (), 1):
            txt = ' '.join(str(sent.get('text') or '').split())
            page = sent.get('page')
            key = (page, txt)
            if not txt or key in seen:
                continue
            seen.add(key)
            is_treat = bool(_METHODS_DEF_RE.search(txt) and _METHODS_CODE_RE.search(txt))
            is_assay = bool(_METHODS_ASSAY_DEF_RE.search(txt))
            if is_treat or is_assay:
                sec = str(sent.get('section_heading') or target.get('section_heading') or '')
                in_methods = bool(re.search(r'\b(?:method|experiment|setup|cultivation|fertiliz|design|analys[ie]s)\b', sec, re.I))
                candidates.append((0 if is_treat else 1, not in_methods, idx, {
                    'page': page,
                    'sentence_number': idx,
                    'section_heading': sec,
                    'text': txt,
                }))
    candidates.sort(key=lambda row: (row[0], row[1], row[2]))
    chosen = [row[3] for row in candidates[:max_sentences]]
    if not chosen:
        return '', []
    formatted = ' '.join(f"[p. {row['page']}] {row['text']}" for row in chosen)
    return formatted, chosen


def attach_treatment_methods_context(targets, max_sentences=3, *, source_targets=None):
    """Attach document-derived treatment methods context and page provenance to targets in place."""
    formatted, prov = extract_treatment_methods_context(source_targets or targets, max_sentences=max_sentences)
    if not formatted:
        return targets
    for target in targets:
        if not target.get('methods_context'):
            target['methods_context'] = formatted
        if not target.get('methods_provenance'):
            target['methods_provenance'] = prov
    return targets


def _candidate_quantity_or_analyte_conflict(
    expanded: str, evidence_pool: str, cand: dict, *, treatment_definition: str = '',
) -> str | None:
    """Reject candidates with physical-quantity, age-quality, disjoint-organ, cross-analyte, or unsupported free-sugar conflicts."""
    iri = cand.get('iri')
    cand_label = (cand.get('label') or '').strip()
    cand_low = cand_label.casefold()
    exp_low = (expanded or '').casefold()
    analyte_context = f'{exp_low} {treatment_definition.casefold()}'

    if _CONCENTRATION_TERM_RE.search(expanded) and not _CONTENT_TERM_RE.search(expanded):
        if _CONTENT_TERM_RE.search(cand_label) and not _CONCENTRATION_TERM_RE.search(cand_label):
            return f"Hard gate: concentration endpoint '{expanded}' cannot map to content class '{cand_label or iri}'."
    if _CONTENT_TERM_RE.search(expanded) and not _CONCENTRATION_TERM_RE.search(expanded):
        if _CONCENTRATION_TERM_RE.search(cand_label) and not _CONTENT_TERM_RE.search(cand_label):
            return f"Hard gate: content endpoint '{expanded}' cannot map to concentration class '{cand_label or iri}'."

    # Disjoint plant-organ pre-filter/gate (prevent tuber/root candidates crowding shoot endpoints, etc.)
    has_root = bool(re.search(r'\b(?:roots?|radicles?)\b', exp_low))
    has_shoot = bool(re.search(r'\b(?:shoots?|aerial|aboveground|above-ground)\b', exp_low))
    has_leaf = bool(re.search(r'\b(?:leaves|leaf|foliar)\b', exp_low))
    has_stem = bool(re.search(r'\b(?:stems?|culms?)\b', exp_low))
    has_tuber = bool(re.search(r'\btubers?\b', exp_low))
    cand_has_tuber = bool(re.search(r'\btubers?\b', cand_low))
    cand_has_root = bool(re.search(r'\b(?:roots?|radicles?)\b', cand_low))
    cand_has_shoot = bool(re.search(r'\bshoots?\b', cand_low)) and not cand_has_tuber
    cand_has_leaf = bool(re.search(r'\b(?:leaves|leaf|foliar)\b', cand_low))
    cand_has_stem = bool(re.search(r'\b(?:stems?|culms?)\b', cand_low))
    if cand_has_tuber and not has_tuber:
        return f"Hard gate: candidate '{cand_label or iri}' specifies tuber structure absent from '{expanded}'."
    if cand_has_root and (has_shoot or has_leaf or has_stem) and not has_root:
        return f"Hard gate: candidate '{cand_label or iri}' specifies root structure absent from '{expanded}'."
    if (cand_has_shoot or cand_has_leaf or cand_has_stem) and has_root and not (has_shoot or has_leaf or has_stem):
        return f"Hard gate: candidate '{cand_label or iri}' specifies non-root structure absent from '{expanded}'."
    if has_shoot and not has_leaf and not has_stem and (cand_has_leaf or cand_has_stem):
        return f"Hard gate: candidate '{cand_label or iri}' specifies leaf/stem rather than shoot system in '{expanded}'."
    if (has_leaf or has_stem) and not has_shoot and cand_has_shoot:
        return f"Hard gate: candidate '{cand_label or iri}' specifies whole shoot system rather than leaf/stem in '{expanded}'."

    if iri in _PATO_AGE_QUALITY_IRIS and _SCALAR_DIMENSION_RE.search(expanded):
        return f"Hard gate: scalar measurement endpoint '{expanded}' cannot collapse onto age quality '{cand_label or iri}'."

    for analyte_word in _SPECIFIC_ANALYTE_WORDS:
        if re.search(rf'\b{re.escape(analyte_word)}\b', cand_low):
            if analyte_word in {'phosphorus', 'phosphate'}:
                has_match = bool(re.search(r'\b(?:phosphorus|phosphate|p|pi|kh2po4|h2po4|hpo4|po4)\b', analyte_context))
            elif analyte_word == 'indole-3-acetic acid':
                has_match = bool(re.search(r'\b(?:indole-3-acetic\s+acid|iaa|auxin)\b', analyte_context))
            else:
                has_match = bool(re.search(rf'\b{re.escape(analyte_word)}\b', analyte_context))
            if not has_match:
                return f"Hard gate: candidate '{cand_label or iri}' specifies analyte '{analyte_word}' not present in endpoint '{expanded}'."

    if 'free sugar' in cand_low and not _FREE_SUGAR_RE.search(f'{expanded} {evidence_pool}'):
        return f"Hard gate: CDNO free sugar class '{cand_label or iri}' requires explicit free-sugar assay evidence."

    return None


def _endpoint_scale_conflict(expanded, local_evidence, cand):
    iri = cand['iri']
    if iri == _TO_SINGLE_ROOT_LENGTH_IRI and _SYSTEM_CUMULATIVE_ROOT_LENGTH_RE.search(expanded):
        return 'Hard gate: TO:0000227 (length of a single root PO:0009005) does not encompass cumulative total root length across a root system.'
    if iri in _TO_AVERAGE_WEIGHT_IRIS and not _AVERAGING_CUE_RE.search(f'{expanded} {local_evidence}'):
        return f"Hard gate: {cand.get('label') or iri} requires explicit averaging evidence (average/mean) matching the TO definition."
    return None


VERIFIER_INSTRUCTIONS = """Verify local ontology class candidates for extracted scientific endpoints.

Rules:
- Read each endpoint's verbatim wording, its `expanded_endpoint` (from verified document abbreviations, e.g. `TRL` -> `Total root length`), its anchored passage and any `methods_context` (with page citations), extracted type, and candidate ontology classes (label, definition, parents, class_source, and any OWL-verified narrow synonym).
- Never accept a candidate on word overlap alone; judge whether the ontology definition accurately includes/represents `expanded_endpoint` in context.
- Choose `exact` ONLY when the candidate class matches the same scientific entity, trait, process, or condition without dropping defining qualifiers or aggregations.
- Choose `broader` when the candidate class genuinely subsumes `expanded_endpoint` without distorting its scientific kind, AND whenever an otherwise valid class omits an aggregation/summary qualifier, a cultivar/genotype/species restriction (`of 'Cardoso'`, `of potato plants`), a dosage/level modifier (`high`, `low`, `HP`, `LP`), or an application mode (`inoculation`). Example: `its shallow and sparsely branched root system` against `PO:0025025` (`root system`) MUST be `broader`, and `HP conditions` against `PECO:0001006` (`phosphate exposure`) MUST be `broader` (not `exact`).
- Individual-root vs. root-system & study-average traits: `TO:0000227` (`root length`, defined as "the length of a root (PO:0009005)") is a 1-D extent quality of an individual root, NOT the cumulative `total root length` (`TRL`) of an entire root system (`PO:0025025`). NEVER map `total root length` or `TRL` to `TO:0000227`; either map as `broader` to `TO:0000985` (`root system morphology trait`, "quality of a root system (PO:0025025)") when present, or leave `unresolved`. Likewise, `TO:0000078` (`root dry weight`, "The average dry weight of the root determined in a study") and `TO:0000578` (`root fresh weight`, "The average fresh weight...") require explicit averaging evidence (`average`, `mean`) in the endpoint or anchored source clause; without averaging support, keep `root dry mass` `unresolved` (or evaluate `TO:0000279` `root weight` only if accurate without study-average distortion).
- PECO phosphorus exposures: `PECO:0007397` (`phosphorus nutrient exposure`) describes exposure to phosphorus as supplement. Bare deficiency/scarcity/shortage statements (`P deficiency`, `P scarcity`, `P shortage`) describe a nutrient-deficiency state, NOT an exposure/supplementation protocol, and MUST stay `unresolved`. An experimental treatment arm (`LP conditions`, `LP treatment`, `HP conditions`, `low P`, `P0.5`, `P2`, `P5`, `P30`) qualifies for `PECO:0007397` ONLY when the supplied passage or `methods_context` establishes that phosphorus was added/supplied; otherwise keep it `unresolved`. Test `PECO:0001046` (`limited phosphate exposure`) ONLY for limited exposure/treatment arms (`low-P`, `LP`, `P0.5`, `P2`)—not bare `P deficiency`—when BOTH limited exposure AND the phosphate chemical form (`phosphate`, `orthophosphate`, `KH2PO4`, etc.) are supported in the supplied evidence or `methods_context`.
- Anatomical subsumption vs. meronymy: a single-organ part (e.g. `PO:0009005` `root`) NEVER subsumes a collective structure (`root network`, `root system`, `root architecture`). Map collective structures only to the collective whole (`PO:0025025` `root system`) when present, or keep `unresolved` if only a part is shortlisted.
- Pre-composed organ-specific trait classes vs. generic parent traits: when an endpoint specifies both a trait/quantity and an anatomical structure (e.g., `P content in shoots`, `shoot phosphorus contents`, `P content in roots`, `root phosphorus contents`) and the shortlist includes a valid pre-composed organ-specific class (`TO:0020114` `shoot system phosphorus content`, `TO:0001124` `root phosphorus content`) alongside the organ-agnostic parent (`TO:0001024` `phosphorus content`), select the pre-composed organ-specific class (`TO:0020114` or `TO:0001124`) rather than the generic parent `TO:0001024`.
- Physical quantity integrity (`concentration` vs. `content`, `dry matter` vs. `dry matter content`): `concentration` (e.g. `mg g-1`) and `content` (e.g. `mg organ-1`) are distinct physical quantities — NEVER map a `concentration` endpoint to a `* content` class (e.g. `TO:0001124` `root phosphorus content` or `TO:0020114` `shoot system phosphorus content`) or a `content` endpoint to a `* concentration` class; leave `unresolved` if the matching quantity is not shortlisted. Map `dry matter` / `DM` to `* dry matter content` at most as `broader` (not `exact`).
- Component quantity fallback (`PATO:0000033` `concentration of`): when a concentration endpoint names an analyte (`sugar concentration`, `soluble sugar concentration`, `IAA concentration`) with no pre-composed matching `<analyte> concentration` class shortlisted (never substitute `* content`, a different analyte, or a `CDNO` `free sugar` class without explicit `"free sugar"` assay evidence), choose `PATO:0000033` (`concentration of`) as `broader` while preserving the analyte and organ in downstream components.
- Choose `unresolved` (with candidate_alias=null) when candidates are ambiguous, merely related (`P acquisition efficiency` vs `phosphorus uptake`, or compound `P foraging by root growth...` vs `growth`), generic element `P` without chemical form, multi-arm contrasts (`HP and LP environments`), subset counts (`21 out of 22 RSA traits`), generic upper classes (`ENVO:01000254`, `PECO:0001001`), cultivars/genotypes/percentages, or broken wording.
- Choose an alternate-type candidate (`A*`) ONLY if type_challenged=true, revised_type equals the candidate's suggested_type, and the anchored sentence clearly establishes that alternate type rather than the extracted type.
- Return one decision object for every input item_id."""

VERIFIER_SCHEMA = {
    '$schema': 'https://json-schema.org/draft/2020-12/schema',
    'type': 'object',
    'additionalProperties': False,
    'required': ['decisions'],
    'properties': {
        'decisions': {
            'type': 'array',
            'items': {
                'type': 'object',
                'additionalProperties': False,
                'required': [
                    'item_id',
                    'decision',
                    'candidate_alias',
                    'type_challenged',
                    'revised_type',
                    'justification',
                ],
                'properties': {
                    'item_id': {'type': 'string', 'minLength': 1},
                    'decision': {'type': 'string', 'enum': ['exact', 'broader', 'unresolved']},
                    'candidate_alias': {'type': ['string', 'null']},
                    'type_challenged': {'type': 'boolean'},
                    'revised_type': {'type': ['string', 'null'], 'enum': [*TYPE_SOURCES, None]},
                    'justification': {'type': 'string', 'minLength': 1},
                },
            },
        },
    },
}


def _enrich_candidate(cand, alias, suggested_type, role_type, endpoint, aliases, db, reference, lookup_dir):
    iri = cand['iri']
    record = db.execute('SELECT info FROM terms WHERE iri=? AND NOT obsolete', (iri,)).fetchone()
    term = json.loads(record[0]) if record else reference.terms.get(iri, {})
    class_source = cand.get('class_source') or term.get('source', '')

    parent_labels = []
    for p_iri in (cand.get('parents') or term.get('parents') or ())[:4]:
        p_rec = db.execute('SELECT info FROM terms WHERE iri=?', (p_iri,)).fetchone()
        if p_rec:
            parent_labels.append(json.loads(p_rec[0]).get('label') or p_iri.rsplit('/', 1)[-1])
        elif p_iri in reference.terms:
            parent_labels.append(reference.terms[p_iri].get('label') or p_iri.rsplit('/', 1)[-1])
        else:
            parent_labels.append(p_iri.rsplit('/', 1)[-1])

    forms, _ = _exact_lookup_forms(endpoint, role_type, aliases)
    keys = {' '.join(words(form)) for form in forms if words(form)}
    owl_narrow = verify_narrow_synonym_in_owl(lookup_dir, iri, class_source, keys) if lookup_dir else None

    entry = {
        'alias': alias,
        'iri': iri,
        'label': cand.get('label') or term.get('label', ''),
        'definition': cand.get('definition') or term.get('definition', ''),
        'parents': parent_labels,
        'namespace': _namespace(iri),
        'class_source': class_source,
        'exact_synonyms': cand.get('exact_synonyms') or term.get('exact_synonyms') or [],
        'type_role': 'permitted' if suggested_type is None else 'whitelisted_alternate',
    }
    if suggested_type is not None:
        entry['suggested_type'] = suggested_type
    if owl_narrow:
        entry['owl_narrow_synonym'] = owl_narrow['verified_narrow_synonym']
    return entry


def prepare_endpoint_verification_items(claims, candidates_by_claim, db, reference, targets_by_id=None,
                                        *, permitted_limit=6, alternate_limit=3):
    """Build verification items for unresolved endpoints that have permitted or whitelisted candidates."""
    lookup_dir = _db_directory(db)
    items = []
    for claim in claims:
        cid = claim['id']
        target = (targets_by_id or {}).get(claim['target_id'], {})
        glossary = target.get('source_abbreviation_definitions', ())
        aliases = _source_aliases(glossary)
        ep_cands = candidates_by_claim.get(cid, {})
        for role in ('subject', 'object'):
            if claim['bindings'][role].get('iri'):
                continue
            ext_type = (claim.get('endpoint_types') or {}).get(role, 'other')
            allowed_ns = TYPE_SOURCES.get(ext_type, set())
            raw_list = ep_cands.get(role) or []

            raw_ep = _normalize_hyphens(claim[role])
            expanded_ep, abbr_hints = _expand(raw_ep, aliases)
            expanded_ep = _normalize_hyphens(expanded_ep)
            methods_ctx = target.get('methods_context', '')
            methods_prov = target.get('methods_provenance') or []
            evidence_pool = ' '.join(
                t for t in (
                    claim.get('source_evidence_span', ''),
                    claim.get('local_clause') or '',
                    methods_ctx,
                ) if t
            )

            perm_pool = [c for c in raw_list if _namespace(c['iri']) in allowed_ns]
            perm_valid = [
                c for c in perm_pool
                if not _candidate_quantity_or_analyte_conflict(expanded_ep, evidence_pool, c)
            ]
            alt_pools = []
            for alt_type in WHITELISTED_TYPE_CHALLENGES.get(ext_type, ()):
                alt_ns = TYPE_SOURCES.get(alt_type, set())
                for c in raw_list:
                    if _namespace(c['iri']) in alt_ns:
                        alt_pools.append((alt_type, c))
            alt_valid = [
                (alt_type, c) for alt_type, c in alt_pools
                if not _candidate_quantity_or_analyte_conflict(expanded_ep, evidence_pool, c)
            ]

            if perm_valid or alt_valid:
                perm_rows = perm_valid[:permitted_limit]
                chosen_alts = alt_valid
            else:
                perm_rows = perm_pool[:1]
                chosen_alts = alt_pools[:1] if not perm_rows else []

            seen_iris = {c['iri'] for c in perm_rows}
            permitted = [
                _enrich_candidate(c, f'C{idx}', None, ext_type, claim[role], aliases, db, reference, lookup_dir)
                for idx, c in enumerate(perm_rows, 1)
            ]

            alternate = []
            for alt_type, c in chosen_alts:
                if c['iri'] in seen_iris:
                    continue
                seen_iris.add(c['iri'])
                alternate.append(
                    _enrich_candidate(c, f'A{len(alternate) + 1}', alt_type, alt_type,
                                      claim[role], aliases, db, reference, lookup_dir)
                )
                if len(alternate) >= alternate_limit:
                    break

            if not (permitted or alternate):
                continue

            item_id = f'{cid}:{role}'
            all_cands = [*permitted, *alternate]
            anchor_quote = ((claim.get('anchors') or {}).get(role) or {}).get('quote') or claim[role]
            if not permitted and ext_type == 'condition or exposure' and methods_ctx and _METHODS_CODE_RE.search(expanded_ep):
                for peco_iri in (_PECO_LIMITED_PHOSPHATE_IRI, _PECO_SUPPLEMENT_IRI, _PECO_PHOSPHATE_IRI):
                    if peco_iri not in seen_iris and peco_iri in reference.terms:
                        seen_iris.add(peco_iri)
                        cand_row = {'iri': peco_iri, **reference.terms[peco_iri], 'class_source': 'peco.owl'}
                        permitted.append(
                            _enrich_candidate(cand_row, f'C{len(permitted) + 1}', None, ext_type,
                                              claim[role], aliases, db, reference, lookup_dir)
                        )
                        all_cands = [*permitted, *alternate]
            items.append({
                'item_id': item_id,
                'claim_id': cid,
                'target_id': claim['target_id'],
                'role': role,
                'endpoint': claim[role],
                'expanded_endpoint': expanded_ep,
                'abbreviation_expansions': abbr_hints,
                'anchor_quote': anchor_quote,
                'extracted_type': ext_type,
                'evidence_span': claim.get('source_evidence_span', ''),
                'local_clause': claim.get('local_clause') or claim.get('source_evidence_span', ''),
                'methods_context': methods_ctx,
                'methods_provenance': methods_prov,
                'review_status': claim.get('review', 'not_flagged'),
                'permitted_candidates': permitted,
                'alternate_type_candidates': alternate,
                'alias_map': {c['alias']: c for c in all_cands},
            })
    return items


def verify_passage_endpoints(items, client, budget=2500):
    """Run one schema-bound candidate verification call per passage (with zero out-of-list IRIs)."""
    by_target = {}
    for item in items:
        by_target.setdefault(item['target_id'], []).append(item)

    decisions = {}
    for index, (target_id, batch) in enumerate(by_target.items(), 1):
        payload = [
            {
                'item_id': item['item_id'],
                'endpoint': item['endpoint'],
                'expanded_endpoint': item.get('expanded_endpoint') or item['endpoint'],
                **({'abbreviation_expansions': item['abbreviation_expansions']} if item.get('abbreviation_expansions') else {}),
                'anchor_quote': item['anchor_quote'],
                'extracted_type': item['extracted_type'],
                'local_clause': item['local_clause'],
                **({'methods_context': item['methods_context']} if item.get('methods_context') else {}),
                'permitted_candidates': [
                    {k: c[k] for k in ('alias', 'label', 'definition', 'parents', 'namespace', 'class_source')
                     if c.get(k)} | ({'owl_narrow_synonym': c['owl_narrow_synonym']} if c.get('owl_narrow_synonym') else {})
                    for c in item['permitted_candidates']
                ],
                'whitelisted_alternate_candidates': [
                    {k: c[k] for k in ('alias', 'suggested_type', 'label', 'definition', 'parents', 'namespace', 'class_source')
                     if c.get(k)} | ({'owl_narrow_synonym': c['owl_narrow_synonym']} if c.get('owl_narrow_synonym') else {})
                    for c in item['alternate_type_candidates']
                ],
            }
            for item in batch
        ]
        user_msg = json.dumps({'items': payload}, ensure_ascii=False, indent=2)
        answer = client.chat_json(
            VERIFIER_INSTRUCTIONS,
            user_msg,
            max_tokens=budget,
            json_schema=VERIFIER_SCHEMA,
            request_label=f'endpoint verification target {index}/{len(by_target)} ({target_id[-8:]})',
        )
        valid_ids = {item['item_id'] for item in batch}
        for dec in answer['decisions']:
            if dec['item_id'] in valid_ids:
                decisions[dec['item_id']] = dec
    return decisions


def _hard_gate_rejection(item, dec, cand, db):
    alias = dec['candidate_alias']
    iri = cand['iri']
    local_id = iri.rsplit('/', 1)[-1]
    ns = cand['namespace']
    ext_type = item['extracted_type']
    name = item['endpoint']
    expanded = item.get('expanded_endpoint') or name

    if _MEASUREMENT_ONLY_RE.fullmatch(name.strip()):
        return 'Hard gate: pure measurement/percentage span cannot be mapped as an ontology class.'
    if _COUNT_SUBSET_RE.search(expanded):
        return 'Hard gate: subset count phrase cannot map as an unrestricted ontology trait class.'
    if local_id in _GENERIC_UPPER_BLOCK_IDS:
        return f'Hard gate: generic upper ontology class {local_id} is too broad for experimental endpoint grounding.'

    if cand['type_role'] == 'permitted':
        if dec['type_challenged'] and dec['revised_type'] and dec['revised_type'] != ext_type:
            return f"Hard gate: permitted candidate {alias} requires extracted_type '{ext_type}'."
        if ns not in TYPE_SOURCES.get(ext_type, set()):
            return f"Hard gate: candidate {alias} namespace '{ns}' violates permitted type '{ext_type}'."
        target_type = ext_type
    else:
        allowed_alts = WHITELISTED_TYPE_CHALLENGES.get(ext_type, ())
        revised = dec['revised_type']
        if not dec['type_challenged'] or revised not in allowed_alts or revised != cand.get('suggested_type'):
            return f"Hard gate: alternate candidate {alias} requires whitelisted type_challenged=true and valid revised_type."
        if ns not in TYPE_SOURCES.get(revised, set()):
            return f"Hard gate: candidate {alias} namespace '{ns}' violates revised type '{revised}'."
        target_type = revised

    evidence_pool = ' '.join(
        t for t in (item.get('evidence_span', ''), item.get('local_clause', ''), item.get('methods_context', '')) if t
    )

    if target_type == 'condition or exposure':
        if _MULTI_ARM_CONTRAST_RE.search(expanded):
            return 'Hard gate: multi-arm treatment contrast cannot collapse onto a single PECO/ENVO exposure class.'
        if iri == _PECO_SUPPLEMENT_IRI:
            if _DEFICIENCY_ONLY_RE.search(expanded):
                return 'Hard gate: deficiency/scarcity/shortage alone does not establish phosphorus supplementation (PECO:0007397).'
            if (item.get('treatment_definition') or _LOW_DOSE_P_RE.search(expanded)
                    or _EXPERIMENTAL_P_ARM_RE.search(expanded)) and not _P_ADDITION_EVIDENCE_RE.search(evidence_pool):
                return 'Hard gate: experimental phosphorus arm lacks passage or methods evidence that phosphorus was added (PECO:0007397).'
        elif iri in {_PECO_LIMITED_PHOSPHATE_IRI, _PECO_PHOSPHATE_IRI}:
            combined = f'{expanded} {evidence_pool}'
            if not (_PHOSPHATE_FORM_RE.search(combined) or _PHOSPHATE_PI_RE.search(combined)):
                return 'Hard gate: PECO phosphate exposure requires explicit source support for the phosphate chemical form.'
            if iri == _PECO_LIMITED_PHOSPHATE_IRI:
                if _DEFICIENCY_ONLY_RE.search(expanded) and not _EXPOSURE_ARM_WORDING_RE.search(expanded):
                    return 'Hard gate: bare deficiency statement stays distinct from an experimental limited-phosphate exposure arm (PECO:0001046).'
                if not _LIMITED_EXPOSURE_RE.search(expanded):
                    return 'Hard gate: PECO:0001046 (limited phosphate exposure) requires limited, low, or absent exposure wording.'

    local_evidence = ' '.join(t for t in (item.get('evidence_span', ''), item.get('local_clause', '')) if t)
    if conflict := _endpoint_scale_conflict(expanded, local_evidence, cand):
        return conflict

    if _EFFICIENCY_ENDPOINT_RE.search(expanded) and not _EFFICIENCY_CLASS_RE.search(
        f"{cand.get('label', '')} {cand.get('definition', '')}"
    ):
        return (
            f"Hard gate: efficiency endpoint '{expanded}' cannot collapse onto non-efficiency "
            f"class '{cand.get('label') or iri}'."
        )

    if len({m.group(0).casefold() for m in _DISTINCT_COORDINATED_TRAIT_RE.finditer(expanded)}) >= 2:
        return f"Hard gate: bundled endpoint '{expanded}' combines multiple distinct traits."

    if conflict := _candidate_quantity_or_analyte_conflict(
        expanded, evidence_pool, cand, treatment_definition=item.get('treatment_definition', ''),
    ):
        return conflict

    if iri in _PATO_RELATIVE_SIZE_IRIS:
        raw_name = str(name or '')
        if (_QUOTED_STUDY_CATEGORY_RE.search(raw_name) or _QUOTED_STUDY_CATEGORY_RE.search(expanded)
                or (_STUDY_CATEGORY_LABEL_RE.search(expanded.strip()) and _STUDY_CATEGORY_CONTEXT_RE.search(evidence_pool))):
            return (
                f"Hard gate: study-defined RSA/size category label ('{raw_name}') must retain the paper's "
                f"category definition rather than collapsing onto PATO relative size quality '{cand.get('label') or iri}'."
            )

    if target_type == 'plant part' and _COLLECTIVE_STRUCTURE_RE.search(expanded):
        rec = db.execute('SELECT info FROM terms WHERE iri=?', (iri,)).fetchone()
        term = json.loads(rec[0]) if rec else {}
        label = str(cand.get('label') or term.get('label', ''))
        definition = str(cand.get('definition') or term.get('definition', ''))
        cand_parents = set(cand.get('parents') or ())
        db_parents = set(term.get('parents') or ())
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

    if target_type == 'chemical':
        rec = db.execute('SELECT info FROM terms WHERE iri=?', (iri,)).fetchone()
        term = json.loads(rec[0]) if rec else {'label': cand['label']}
        if _is_blocked_elemental_chemical(iri, term, name, [item['evidence_span']]):
            return 'Generic element mention lacks a specified chemical form; atomic CHEBI classes are withheld.'

    if target_type == 'organism' and _CULTIVAR_OR_GENOTYPE_RE.search(expanded):
        return 'Hard gate: cultivar/genotype mention cannot collapse to a species-level taxon.'

    return None


def _enforce_broader_if_qualified(item, dec, cand):
    """Demote exact -> broader when the class omits aggregation, cultivar, or descriptive qualifiers."""
    if dec['decision'] != 'exact':
        return dec['decision'], []
    expanded = _rejoin_pdf_word_hyphens(item.get('expanded_endpoint') or item['endpoint'])
    cand_label = (cand.get('label') or '').casefold()
    cand_def = (cand.get('definition') or '').casefold()

    lost = []
    for tok in _AGGREGATION_MODIFIER_RE.findall(expanded):
        stem = tok.casefold()
        if stem not in cand_label and stem not in cand_def:
            lost.append(tok)
    if (_CULTIVAR_OR_GENOTYPE_RE.search(expanded) or _ORGANISM_RESTRICT_RE.search(expanded)) and 'cultivar' not in cand_label and 'potato' not in cand_label:
        lost.append('organism/cultivar scope')
    for tok in _DESCRIPTIVE_MODIFIER_RE.findall(expanded):
        if tok.casefold() not in cand_label and tok.casefold() not in cand_def:
            lost.append(tok)
    if _DRY_MATTER_RE.search(expanded) and not _CONTENT_TERM_RE.search(expanded) and _DRY_MATTER_CONTENT_RE.search(cand_label):
        lost.append('dry matter vs. dry matter content')
    if cand.get('iri') == _PATO_CONCENTRATION_IRI:
        if any(w not in {'concentration', 'concentrations', 'of', 'in'} for w in words(expanded)):
            lost.append('analyte/substance specificity')

    effective_type = dec.get('revised_type') or item.get('extracted_type')
    if effective_type in {'trait or measurement', 'biological process', 'assay variable'} and cand.get('namespace') != 'PO':
        cand_names = ' '.join([cand.get('label') or '', *(cand.get('exact_synonyms') or ())]).casefold()
        for organ_name, organ_re, organ_terms in _PLANT_ORGAN_LOCATIONS:
            if organ_re.search(expanded) and not any(t in cand_names for t in organ_terms):
                lost.append(f'organ location ({organ_name})')

    lost = list(dict.fromkeys(lost))
    if lost:
        return 'broader', lost
    return 'exact', []


def apply_verified_decisions(claims, decisions_by_item, items_by_id, db, reference):
    """Apply post-validated verifier endpoint decisions without mutating held status or relaxing relation rules."""
    updated = deepcopy(claims)
    for claim in updated:
        cid = claim['id']
        for role in ('subject', 'object'):
            item_id = f'{cid}:{role}'
            if item_id not in items_by_id:
                continue
            item = items_by_id[item_id]
            dec = decisions_by_item.get(item_id)
            if not dec or dec['decision'] == 'unresolved' or not dec['candidate_alias']:
                if dec and dec.get('justification'):
                    claim['bindings'][role] = {
                        **claim['bindings'][role],
                        'iri': None, 'label': None, 'mapping': 'unresolved',
                        'verifier_justification': dec['justification'],
                    }
                continue

            cand = item['alias_map'].get(dec['candidate_alias'])
            if cand is None:
                claim['bindings'][role] = {
                    'iri': None, 'label': None, 'mapping': 'unresolved',
                    'reason': 'Hard gate rejected out-of-list candidate alias.',
                    'verifier_justification': dec.get('justification'),
                }
                continue

            if gate_err := _hard_gate_rejection(item, dec, cand, db):
                claim['bindings'][role] = {
                    'iri': None, 'label': None, 'mapping': 'unresolved',
                    'reason': gate_err,
                    'verifier_justification': dec.get('justification'),
                }
                continue

            type_challenged = cand['type_role'] == 'whitelisted_alternate'
            revised_type = dec['revised_type'] if type_challenged else None
            final_mapping, lost_qualifiers = _enforce_broader_if_qualified(item, dec, cand)
            reason = dec['justification']
            if lost_qualifiers:
                reason = f"{reason} [Downgraded from exact to broader: class omits qualifier(s): {', '.join(lost_qualifiers)}]"
            claim['bindings'][role] = {
                'iri': cand['iri'],
                'label': cand['label'],
                'mapping': final_mapping,
                'reason': reason,
                'provenance': {
                    'class_source': cand['class_source'],
                    **({'owl_narrow_synonym': cand['owl_narrow_synonym']} if cand.get('owl_narrow_synonym') else {}),
                    **({'methods_provenance': item['methods_provenance']}
                       if item.get('extracted_type') == 'condition or exposure' and item.get('methods_provenance') else {}),
                },
                'type_challenged': type_challenged,
                'revised_type': revised_type,
                'downgraded_from_exact': bool(lost_qualifiers),
                'verifier_justification': dec['justification'],
            }

        # Re-run identical Phase A relation & semantic consistency gates on the final endpoints.
        anchors = claim.get('anchors') or {}
        rel_quote = ((anchors.get('relation') or {}).get('resolved_span')
                     or (anchors.get('relation') or {}).get('quote') or '')
        s_span = ((anchors.get('subject') or {}).get('resolved_span') or claim['subject'])
        o_span = ((anchors.get('object') or {}).get('resolved_span') or claim['object'])
        local_clause = claim.get('local_clause') or claim.get('source_evidence_span', '')

        claim['bindings']['relation'] = resolve_relation(
            claim['relation'],
            reference,
            assertion_status=claim.get('assertion_status', 'affirmed'),
            relation_quote=rel_quote,
            evidence_text=claim.get('source_evidence_span', ''),
            subject_span=s_span,
            object_span=o_span,
            local_clause=local_clause,
        )

        projected = _reference_for_bindings(db, reference, claim['bindings'])
        assessment = {'original_claim': claim, 'bindings': claim['bindings']}
        for issue in mapping_meaning_warnings(assessment, projected):
            claim['bindings'][issue['role']] = {
                'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': issue['message'],
            }

        annotate_endpoint_identities(claim, claim['bindings'])

        if claim['bindings']['relation']['iri']:
            typing = check_property_types(assessment, projected)
            relation_issue = relation_evidence_issue(claim['relation'], claim['bindings']['relation'], projected)
            is_self_link, self_prov, self_err = relation_self_link_status(claim, claim['bindings'])
            rel_iri = claim['bindings']['relation']['iri']
            if is_self_link:
                claim['bindings']['relation'] = {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': self_err}
            elif rel_iri in _DIRECTIONAL_CAUSAL_IRIS:
                both_resolved = bool(claim['bindings']['subject']['iri'] and claim['bindings']['object']['iri'])
                if not both_resolved or typing['status'] != 'compatible' or relation_issue:
                    reason = relation_issue or f"Causal property requires compatible resolved endpoint types ({typing['status']})."
                    claim['bindings']['relation'] = {'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': reason}
                else:
                    claim['bindings']['relation']['provenance'] = {
                        **(claim['bindings']['relation'].get('provenance') or {}), **self_prov,
                    }
            elif typing['status'] not in {'compatible', 'not_applicable'} or relation_issue:
                claim['bindings']['relation'] = {
                    'iri': None, 'label': None, 'mapping': 'unresolved',
                    'reason': relation_issue or typing['status'],
                }
            else:
                claim['bindings']['relation']['provenance'] = {
                    **(claim['bindings']['relation'].get('provenance') or {}), **self_prov,
                }

        hold_reasons = claim.get('hold_reasons') or []
        claim['classified'] = (
            not hold_reasons
            and claim.get('review') != 'held'
            and all(binding.get('iri') for binding in claim['bindings'].values())
        )
    return updated


def prepare_relation_verification_items(claims, db, reference):
    """Build definition-based relation verification items only for eligible unresolved-relation claims."""
    items = []
    for claim in claims:
        rel_binding = claim['bindings']['relation']
        if rel_binding.get('iri'):
            continue
        subj_iri = claim['bindings']['subject'].get('iri')
        obj_iri = claim['bindings']['object'].get('iri')
        if not (subj_iri and obj_iri):
            continue
        if claim.get('assertion_status', 'affirmed') != 'affirmed' or claim.get('review') == 'held':
            continue
        anchors = claim.get('anchors') or {}
        rel_quote = ((anchors.get('relation') or {}).get('resolved_span')
                     or (anchors.get('relation') or {}).get('quote') or '')
        local_clause = claim.get('local_clause') or claim.get('source_evidence_span', '')
        rel_text = claim['relation']
        if (_QUALIFIED_RELATION.search(rel_text) or _QUALIFIED_RELATION.search(rel_quote)
                or _NULL_EFFECT_RE.search(local_clause)
                or _COMPARATIVE_THAN_RE.search(rel_text)
                or _OBSERVATION_CONDITION_REL_RE.search(rel_text)):
            continue
        projected = _reference_for_bindings(db, reference, claim['bindings'])
        endpoints = {}
        for role in ('subject', 'object'):
            ep = claim['bindings'][role]
            iri = ep['iri']
            term = projected.terms.get(iri) or reference.terms.get(iri) or {}
            endpoints[role] = {
                'phrase': claim[role],
                'iri': iri,
                'label': ep.get('label') or term.get('label'),
                'mapping': ep.get('mapping'),
                'definition': term.get('definition', ''),
                'upper_types': verifiable_upper_types(projected, iri),
            }
        items.append({
            'claim_id': claim['id'],
            'target_id': claim.get('target_id', ''),
            'subject': endpoints['subject'],
            'relation_phrase': rel_text,
            'relation_quote': rel_quote or rel_text,
            'object': endpoints['object'],
            'qualifiers': claim.get('qualifiers') or {},
            'assertion_status': claim.get('assertion_status', 'affirmed'),
            'source_evidence_span': claim.get('source_evidence_span', ''),
            'local_clause': local_clause,
        })
    return items


def verify_unresolved_relations(items, client, reference, *, batch_size=3, budget=4096):
    """Run definition-based relation verification as a bounded fallback on unresolved candidates."""
    if not items:
        return {}
    _, _, _, relations, relation_iris = catalog_term_map(reference)
    relation_section = GROUNDING_INSTRUCTIONS.split('3. RELATION.', 1)[1].split('4. CONSISTENCY', 1)[0]
    system = (
        'Verify ontology object properties ONLY for unresolved scientific relations whose endpoints are already grounded.\n'
        'Select ONLY from supplied approved properties (`relations`) or return mapping="unresolved" with term=null.\n'
        'Evaluate full ontology definitions, domain/range (direct and inherited from parent_properties), BFO upper_types, usage_notes, direction, polarity, and source clause evidence across all supplied properties—never apply blanket verb rules.\n\n'
        '3. RELATION.' + relation_section + '\n'
        '- Match domain/range & BFO `upper_types` uniformly across property families (process-to-process, continuant-to-continuant `RO:0002566`, and process-to-characteristic properties when supplied in `relations`), verifying source causal/increase/decrease evidence rather than mere co-occurrence or observational conditions.\n'
        '- Distinguish process-to-process `RO:0002211` (`regulates`, which requires varying execution on an enabler of another process) from process-to-characteristic `RO:0019000/1/2` (whose subproperty definition is whether process P results in the existence of or an increase/decrease in the intensity or magnitude of characteristic C).\n'
        '- Choose `broader` (not `exact`) whenever source wording omits statistical/polarity/strength qualifiers (`significant`, `positive`, `negative`, `strong`) or uses neutral `affects`/`affected` for `causally influences`, and note `RO:0002410` (`causally related to`) permits `broader` or `unresolved` only.\n'
        '- Return `unresolved` for comparisons, observational changes under conditions (`decreased under LP`), hedges (`can`/`may`), non-significant results, or reversed clauses.'
    )
    exact_terms = [k for k, t in relations.items() if 'exact' in t.get('allowed_mappings', ('exact', 'broader', 'unresolved'))]
    decisions = {}
    for batch_num, start in enumerate(range(0, len(items), batch_size), 1):
        batch = items[start:start + batch_size]
        claims_map = {
            f'C{idx}': {
                'claim_id': it['claim_id'],
                'subject': it['subject'],
                'relation_phrase': it['relation_phrase'],
                'object': it['object'],
                'qualifiers': it['qualifiers'],
                'assertion_status': it['assertion_status'],
                'source_evidence_span': it['source_evidence_span'],
                'local_clause': it['local_clause'],
            }
            for idx, it in enumerate(batch, 1)
        }
        keys = list(claims_map)
        schema = {
            'type': 'object',
            'properties': {
                'results': {
                    'type': 'array',
                    'minItems': len(keys),
                    'maxItems': len(keys),
                    'items': {
                        'type': 'object',
                        'properties': {
                            'claim_id': {'type': 'string', 'enum': keys},
                            'reason': {'type': 'string', 'minLength': 1, 'maxLength': 600},
                            'mapping': {'type': 'string', 'enum': ['exact', 'broader', 'unresolved']},
                            'term': {'type': ['string', 'null'], 'enum': [None, *sorted(relations)]},
                        },
                        'allOf': [{
                            'if': {'properties': {'mapping': {'const': 'exact'}}, 'required': ['mapping']},
                            'then': {'properties': {'term': {'enum': exact_terms}}},
                        }],
                        'required': ['claim_id', 'reason', 'mapping', 'term'],
                        'additionalProperties': False,
                    },
                },
            },
            'required': ['results'],
            'additionalProperties': False,
        }
        user = json.dumps({'relations': relations, 'claims': claims_map}, ensure_ascii=False)
        answer = client.chat_json(
            system, user, json_schema=schema, max_tokens=budget,
            request_label=f'relation verification batch {batch_num}',
        )
        by_key = {row['claim_id']: row for row in answer.get('results') or ()}
        for idx, it in enumerate(batch, 1):
            sel = by_key.get(f'C{idx}')
            if sel:
                alias = sel.get('term')
                iri = relation_iris.get(alias) if alias else None
                decisions[it['claim_id']] = {
                    'iri': iri,
                    'label': reference.terms[iri]['label'] if iri else None,
                    'mapping': sel['mapping'] if iri else 'unresolved',
                    'reason': sel['reason'],
                }
    return decisions


def _passive_reversal_ok(claim, local_clause, s_span, o_span):
    if claim.get('passive_role_reversal_verified') is True:
        return True
    if claim.get('passive_role_reversal_verified') is False:
        return False
    m = _PASSIVE_BY_RE.search(local_clause or '')
    if not m:
        return False
    prefix, suffix = local_clause[:m.start()], local_clause[m.end():]
    obj_before = bool(source_surface_mention(prefix, o_span))
    subj_after = bool(source_surface_mention(suffix, s_span))
    subj_before = bool(source_surface_mention(prefix, s_span))
    obj_after = bool(source_surface_mention(suffix, o_span))
    return obj_before and subj_after and not (subj_before and obj_after)


def _relation_fallback_gate_rejection(claim, item, iri, mapping, projected):
    anchors = claim.get('anchors') or {}
    rel_quote = ((anchors.get('relation') or {}).get('resolved_span')
                 or (anchors.get('relation') or {}).get('quote') or item.get('relation_quote', ''))
    s_span = ((anchors.get('subject') or {}).get('resolved_span') or claim['subject'])
    o_span = ((anchors.get('object') or {}).get('resolved_span') or claim['object'])
    local_clause = claim.get('local_clause') or claim.get('source_evidence_span', '')

    if claim.get('assertion_status', 'affirmed') != 'affirmed':
        return f"Hard gate: non-affirmed assertion_status '{claim.get('assertion_status')}' stays unresolved."
    if (_QUALIFIED_RELATION.search(claim['relation']) or _QUALIFIED_RELATION.search(rel_quote)
            or _NULL_EFFECT_RE.search(local_clause)
            or _COMPARATIVE_THAN_RE.search(claim['relation'])
            or _OBSERVATION_CONDITION_REL_RE.search(claim['relation'])):
        return 'Hard gate: hedged, non-significant, comparative, or observational condition wording stays unresolved.'

    pred_sign = _polarity_sign(claim['relation'])
    quote_sign = _polarity_sign(rel_quote or local_clause)
    if pred_sign and quote_sign and pred_sign != quote_sign:
        return f"Hard gate: predicate polarity ({pred_sign}) contradicts source quote ({quote_sign})."
    if _has_reversed_active_order(local_clause, s_span, o_span, claim['relation']):
        return 'Hard gate: reversed active clause order does not establish forward subject -> object relation.'

    is_passive = bool(_PASSIVE_BY_RE.search(claim['relation']) or _PASSIVE_BY_RE.search(rel_quote)
                      or claim.get('passive_source_evidence'))
    if claim.get('proposed_relation_iri') == iri:
        if claim.get('passive_role_reversal_verified') is False:
            return (
                f"Hard gate: passive relation wording ('{claim['relation']}') was not reversed into forward subject -> object order."
            )
        if is_passive and not _passive_reversal_ok(claim, local_clause, s_span, o_span):
            return (
                f"Hard gate: passive relation wording ('{claim['relation']}') requires verified passive role reversal."
            )
    elif is_passive and iri in _FORWARD_ACTIVE_CAUSAL_IRIS:
        return (
            f"Hard gate: passive relation wording ('{claim['relation']}') requires an inverse property "
            '(e.g., causally influenced by RO:0002559) or reversed endpoint order.'
        )
    if not is_passive and iri == _RO_CAUSALLY_INFLUENCED_BY_IRI:
        return 'Hard gate: inverse property causally influenced by (RO:0002559) requires passive relation wording.'

    if iri in {_RO_0019001_IRI, 'http://purl.obolibrary.org/obo/RO_0019002'}:
        obj_phrase = str(claim.get('object') or '')
        if _NON_SCALAR_MORPHOLOGY_RE.search(obj_phrase) and not _SCALAR_DIMENSION_RE.search(obj_phrase):
            return (
                f"Hard gate: '{claim['object']}' is a general morphology category rather than a scalar "
                'quantitative characteristic whose intensity/magnitude increases or decreases.'
            )

    both_resolved = bool(claim['bindings']['subject'].get('iri') and claim['bindings']['object'].get('iri'))
    if not both_resolved:
        return 'Hard gate: binary relation requires resolved subject and object endpoints.'

    term = projected.terms.get(iri, {})
    if prop_sign := property_polarity_sign(iri, term):
        quote_polarity = _polarity_sign(rel_quote) or _polarity_sign(local_clause)
        if pred_sign != prop_sign:
            return (
                f"Hard gate: signed property '{term.get('label') or iri}' ({prop_sign}) "
                f"conflicts with predicate polarity ({pred_sign or 'neutral'})."
            )
        if quote_polarity and quote_polarity != prop_sign:
            return (
                f"Hard gate: signed property '{term.get('label') or iri}' ({prop_sign}) "
                f"conflicts with source clause polarity ({quote_polarity})."
            )

    if mapping not in term.get('allowed_mappings', ('exact', 'broader', 'unresolved')):
        return f"Hard gate: {term.get('label') or iri} does not permit mapping '{mapping}'."

    assessment = {
        'original_claim': claim,
        'bindings': {**claim['bindings'], 'relation': {'iri': iri, 'label': term.get('label'), 'mapping': mapping}},
    }
    typing = check_property_types(assessment, projected)
    if typing['status'] in {'incompatible', 'missing_required_endpoint'}:
        return f"Hard gate: relation endpoint types {typing['status']} for {term.get('label') or iri}."
    requires_strict_compat = iri in _DIRECTIONAL_CAUSAL_IRIS or iri in _RO_REGULATES_CHAR_IRIS or iri in {
        _RO_CAUSALLY_INFLUENCES_IRI, _RO_CAUSALLY_INFLUENCED_BY_IRI,
    }
    if requires_strict_compat and typing['status'] != 'compatible':
        return f"Hard gate: {term.get('label') or iri} requires compatible verified endpoint types ({typing['status']})."

    if issue := relation_evidence_issue(claim['relation'], {'iri': iri, 'mapping': mapping}, projected):
        return issue
    is_self_link, _, self_err = relation_self_link_status(claim, claim['bindings'])
    if is_self_link:
        return self_err
    return None


def apply_relation_verification_decisions(claims, decisions_by_id, items_by_id, db, reference):
    """Apply post-checked experimental relation-verifier decisions without altering held or endpoint state."""
    updated = deepcopy(claims)
    for claim in updated:
        cid = claim['id']
        if cid not in items_by_id or claim['bindings']['relation'].get('iri'):
            continue
        dec = decisions_by_id.get(cid)
        if not dec or dec.get('mapping') == 'unresolved' or not dec.get('iri'):
            continue
        iri = dec['iri']
        projected = _reference_for_bindings(db, reference, claim['bindings'])
        term = projected.terms.get(iri) or reference.terms.get(iri) or {}
        mapping = dec['mapping']
        downgraded = False
        if mapping == 'exact' and not direct_property_allowed(iri):
            mapping, downgraded = 'broader', True
        elif mapping == 'exact' and (
            _RELATION_MODIFIER_LOSS_RE.search(claim['relation'])
            or (iri == _RO_CAUSALLY_INFLUENCES_IRI and _NEUTRAL_INFLUENCE_VERB_RE.search(claim['relation']))
        ):
            mapping, downgraded = 'broader', True

        if err := _relation_fallback_gate_rejection(claim, items_by_id[cid], iri, mapping, projected):
            claim['bindings']['relation'] = {
                'iri': None, 'label': None, 'mapping': 'unresolved', 'reason': err,
                'verifier_justification': dec.get('reason'),
            }
            continue

        direct_allowed = direct_property_allowed(iri)
        _, self_prov, _ = relation_self_link_status(claim, claim['bindings'])
        reason = dec.get('reason') or term.get('label', '')
        if downgraded:
            reason = f'{reason} [Downgraded from exact to broader: property omits qualifier or mechanism specificity.]'
        claim['bindings']['relation'] = {
            'iri': iri,
            'label': term.get('label') or dec.get('label'),
            'mapping': mapping,
            'reason': reason,
            'provenance': {
                'fallback_verifier': True,
                'direct_use_allowed': direct_allowed,
                'grouping_only': not direct_allowed,
                **self_prov,
            },
            'downgraded_from_exact': downgraded,
            'verifier_justification': dec.get('reason'),
        }
        hold_reasons = claim.get('hold_reasons') or []
        claim['classified'] = (
            not hold_reasons
            and claim.get('review') != 'held'
            and all(binding.get('iri') for binding in claim['bindings'].values())
        )
    return updated


_RO_PROCESS_CHAR_PILOT_TTL = (
    '@prefix obo: <http://purl.obolibrary.org/obo/> .\n'
    '@prefix owl: <http://www.w3.org/2002/07/owl#> .\n'
    '@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .\n'
    '@prefix terms: <http://purl.org/dc/terms/> .\n\n'
    'obo:RO_0019000 a owl:ObjectProperty ;\n'
    '    rdfs:label "regulates characteristic" ;\n'
    '    obo:IAO_0000115 "A relationship that holds between a process and a characteristic in which process (P) regulates characteristic (C) iff:\xa0P results in the existence of C OR affects the intensity or magnitude of C." ;\n'
    '    terms:contributor <https://orcid.org/0000-0002-8688-6599> ;\n'
    '    rdfs:subPropertyOf obo:RO_0002410 ;\n'
    '    rdfs:domain obo:BFO_0000015 ;\n'
    '    rdfs:range obo:PATO_0000001 .\n\n'
    'obo:RO_0019001 a owl:ObjectProperty ;\n'
    '    rdfs:label "positively regulates characteristic" ;\n'
    '    obo:IAO_0000115 "A relationship that holds between a process and a characteristic in which process (P) positively regulates characteristic (C) iff:\xa0P results in an increase in the intensity or magnitude of C." ;\n'
    '    terms:contributor <https://orcid.org/0000-0002-8688-6599> ;\n'
    '    rdfs:subPropertyOf obo:RO_0019000 .\n\n'
    'obo:RO_0019002 a owl:ObjectProperty ;\n'
    '    rdfs:label "negatively regulates characteristic" ;\n'
    '    obo:IAO_0000115 "A relationship that holds between a process and a characteristic in which process (P) negatively regulates characteristic (C) iff:\xa0P results in a decrease in the intensity or magnitude of C." ;\n'
    '    terms:contributor <https://orcid.org/0000-0002-8688-6599> ;\n'
    '    rdfs:subPropertyOf obo:RO_0019000 .\n'
).encode('utf-8')


def experimental_relation_catalog(base_reference, cache_dir):
    """Build an isolated 24-property catalog (+RO:0019001) without mutating the production 23-property catalog."""
    if 'http://purl.obolibrary.org/obo/RO_0019001' in base_reference.profile['allowed_object_properties']:
        return base_reference
    dest = Path(cache_dir)
    if (dest / 'profile.json').is_file() and (dest / 'manifest.json').is_file():
        return Catalog(dest)
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(base_reference.directory, dest)
    frag_path = dest / 'fragments' / 'ro_process_characteristic_pilot.ttl'
    frag_path.write_bytes(_RO_PROCESS_CHAR_PILOT_TTL)
    manifest = json.loads((dest / 'manifest.json').read_text(encoding='utf-8'))
    manifest['modules'].append({
        'ontology': 'RO',
        'file': 'fragments/ro_process_characteristic_pilot.ttl',
        'format': 'turtle',
        'source': 'https://raw.githubusercontent.com/oborel/obo-relations/master/src/ontology/ro-edit.owl',
        'sha256': hashlib.sha256(_RO_PROCESS_CHAR_PILOT_TTL).hexdigest(),
        'term_iris': [
            'http://purl.obolibrary.org/obo/RO_0019000',
            _RO_0019001_IRI,
            'http://purl.obolibrary.org/obo/RO_0019002',
        ],
    })
    (dest / 'manifest.json').write_text(json.dumps(manifest, indent=2), encoding='utf-8')
    profile = json.loads((dest / 'profile.json').read_text(encoding='utf-8'))
    if _RO_0019001_IRI not in profile['allowed_object_properties']:
        profile['allowed_object_properties'].append(_RO_0019001_IRI)
    profile['experiment_note'] = 'Isolated experimental 24-property catalog (+RO:0019001); production stays at 23 properties.'
    (dest / 'profile.json').write_text(json.dumps(profile, indent=2), encoding='utf-8')
    return Catalog(dest)


def verify_single_stage_output(output, reference, client, progress, lookup_dir, *, method_targets=None):
    """Run experimental verifier v2 over saved single-stage claims and rewrite claims.jsonl."""
    claims_path = output / 'claims.jsonl'
    claims = read_jsonl(claims_path)
    if not claims:
        progress(verifier_policy=POLICY, verified_items=0, classified_count=0)
        return claims
    targets = read_jsonl(output / 'targets.jsonl')
    attach_treatment_methods_context(targets, source_targets=method_targets)
    targets_by_id = {row['target_id']: row for row in targets}
    cands_by_claim = {
        row['claim_id']: row['endpoints']
        for row in read_jsonl(output / 'endpoint_candidates.jsonl')
    }
    exp_reference = experimental_relation_catalog(reference, output / '_experimental_catalog_24')
    db, _ = open_lookup(lookup_dir)
    rel_items_count = 0
    with closing(db):
        items = prepare_endpoint_verification_items(claims, cands_by_claim, db, reference, targets_by_id=targets_by_id)
        if items:
            decisions = verify_passage_endpoints(items, client)
            updated = apply_verified_decisions(claims, decisions, {it['item_id']: it for it in items}, db, reference)
        else:
            updated = claims
        rel_items = prepare_relation_verification_items(updated, db, exp_reference)
        rel_items_count = len(rel_items)
        if rel_items:
            rel_decisions = verify_unresolved_relations(rel_items, client, exp_reference)
            updated = apply_relation_verification_decisions(
                updated, rel_decisions, {it['claim_id']: it for it in rel_items}, db, exp_reference,
            )
    save_rows(claims_path, updated)
    progress(
        verifier_policy=POLICY,
        verifier_catalogue_properties=len(exp_reference.profile['allowed_object_properties']),
        verified_items=len(items),
        verified_relations=rel_items_count,
        classified_count=sum(1 for row in updated if row.get('classified')),
    )
    return updated
