"""Extraction response schema, limits and conservative lexical artifact guards."""

from __future__ import annotations

from copy import deepcopy
from functools import lru_cache
import hashlib
import json
import re


GENERIC_ENTITIES = {"agriculture", "food", "temperature", "yield", "disease", "soil", "pests", "crop", "health", "nutrition", "nutrient",
                    "crop growth", "crop productivity", "soil health", "soil quality", "nutrient levels",
                    "plantdisease", "soilcondition", "climatefactor", "humanexposure", "humanhealthoutcome", "agriculturalpractice"}


EXTRACTION_MAX_CLAIMS_PER_PACKET = 0  # No application claim-count cap by default.
EXTRACTION_CONTRACT_VERSION = 20
CLOSED_EXTRACTION_CONTRACT_VERSION = 26
MEASURED_OBSERVATION_CONTRACT_VERSION = 23
CANONICAL_PREDICATE_CONTRACT_VERSION = 24
SAVED_CLOSED_EXTRACTION_VERSIONS = (
    22, MEASURED_OBSERVATION_CONTRACT_VERSION, CANONICAL_PREDICATE_CONTRACT_VERSION, 25, CLOSED_EXTRACTION_CONTRACT_VERSION,
)
CLOSED_PREDICATE_SET_VERSION = 'closed-ro-bfo-12-v1'
LEGACY_CLOSED_PREDICATE_SET_VERSIONS = frozenset({'closed-ro-bfo-9-v1'})
COMPATIBLE_CLOSED_PREDICATE_SET_VERSIONS = LEGACY_CLOSED_PREDICATE_SET_VERSIONS | {CLOSED_PREDICATE_SET_VERSION}
LEGACY_CLOSED_WHITELIST_DIGESTS = frozenset({'a638c3dd0f8ea56296ca190953c16d2356e629f2265631ccb70ac505a8650370'})

CLOSED_PREDICATES = {
    'P1': {
        'alias': 'P1',
        'id': 'RO:0019001',
        'iri': 'http://purl.obolibrary.org/obo/RO_0019001',
        'display_label': 'positively regulates characteristic',
        'admission_rule': 'A process or experimental intervention (in subject) explicitly causes a characteristic (in object) to increase in intensity or magnitude. Bare organisms, cultivars, chemicals, or organs cannot be the subject of P1.',
        'directed': True,
        'supports_passive_reversal': True,
        'polarity': 'positive',
    },
    'P2': {
        'alias': 'P2',
        'id': 'RO:0019002',
        'iri': 'http://purl.obolibrary.org/obo/RO_0019002',
        'display_label': 'negatively regulates characteristic',
        'admission_rule': 'A process or experimental intervention (in subject) explicitly causes a characteristic (in object) to decrease in intensity or magnitude. Bare organisms, cultivars, chemicals, or organs cannot be the subject of P2.',
        'directed': True,
        'supports_passive_reversal': True,
        'polarity': 'negative',
    },
    'P3': {
        'alias': 'P3',
        'id': 'RO:0002566',
        'iri': 'http://purl.obolibrary.org/obo/RO_0002566',
        'display_label': 'causally influences',
        'admission_rule': 'The source explicitly asserts a causal effect between eligible continuant endpoints. Statistical significance or factor-level variation alone is insufficient.',
        'directed': True,
        'supports_passive_reversal': True,
        'polarity': None,
    },
    'P4': {
        'alias': 'P4',
        'id': 'RO:0002411',
        'iri': 'http://purl.obolibrary.org/obo/RO_0002411',
        'display_label': 'causally upstream of',
        'admission_rule': 'The source explicitly asserts a causal link between occurrents (processes) with the required upstream definition.',
        'directed': True,
        'supports_passive_reversal': True,
        'polarity': None,
    },
    'P5': {
        'alias': 'P5',
        'id': 'RO:0002610',
        'iri': 'http://purl.obolibrary.org/obo/RO_0002610',
        'display_label': 'correlated with',
        'admission_rule': 'The source explicitly reports statistical dependence or correlation between two variables (symmetric relationship).',
        'directed': False,
        'supports_passive_reversal': False,
        'polarity': None,
    },
    'P6': {
        'alias': 'P6',
        'id': 'RO:0003000',
        'iri': 'http://purl.obolibrary.org/obo/RO_0003000',
        'display_label': 'produces',
        'admission_rule': 'A material entity produces a material output through a process occurring in that producer.',
        'directed': True,
        'supports_passive_reversal': True,
        'polarity': None,
    },
    'P7': {
        'alias': 'P7',
        'id': 'BFO:0000051',
        'iri': 'http://purl.obolibrary.org/obo/BFO_0000051',
        'display_label': 'has part',
        'admission_rule': 'The source asserts whole-to-part composition (put the whole in subject and the part in object).',
        'directed': True,
        'supports_passive_reversal': True,
        'polarity': None,
    },
    'P8': {
        'alias': 'P8',
        'id': 'RO:0002233',
        'iri': 'http://purl.obolibrary.org/obo/RO_0002233',
        'display_label': 'has input',
        'admission_rule': 'A process (in subject, including dietary intake or ingestion) has the source-described material continuant input (in object, such as a food, nutrient, or food additive). Do not use P3 when the object is an intake or exposure process.',
        'directed': True,
        'supports_passive_reversal': True,
        'polarity': None,
    },
    'P9': {
        'alias': 'P9',
        'id': 'RO:0002234',
        'iri': 'http://purl.obolibrary.org/obo/RO_0002234',
        'display_label': 'has output',
        'admission_rule': 'A process (in subject) has the specified continuant output (in object).',
        'directed': True,
        'supports_passive_reversal': True,
        'polarity': None,
    },
    'P10': {
        'alias': 'P10',
        'id': 'RO:0003302',
        'iri': 'http://purl.obolibrary.org/obo/RO_0003302',
        'display_label': 'causes or contributes to condition',
        'admission_rule': 'An explicit exposure, chemical/substance, or biological factor (in subject) causally produces or contributes to a stated disease or abnormal phenotype/condition (in object). Never use P10 for neutral physiological measurements (use P1/P2), mere epidemiological associations without causal assertion, or hedged possibility.',
        'directed': True,
        'supports_passive_reversal': True,
        'polarity': None,
    },
    'P11': {
        'alias': 'P11',
        'id': 'RO:0002213',
        'iri': 'http://purl.obolibrary.org/obo/RO_0002213',
        'display_label': 'positively regulates',
        'admission_rule': 'A process or intervention (in subject) explicitly increases the rate or magnitude of execution of a biological process (in object), such as transport, secretion, excretion, or synthesis. Both subject and object must be processes.',
        'directed': True,
        'supports_passive_reversal': True,
        'polarity': 'positive',
    },
    'P12': {
        'alias': 'P12',
        'id': 'RO:0002212',
        'iri': 'http://purl.obolibrary.org/obo/RO_0002212',
        'display_label': 'negatively regulates',
        'admission_rule': 'A process or intervention (in subject) explicitly decreases the rate or magnitude of execution of a biological process (in object), such as transport, secretion, excretion, synthesis, or reabsorption. Both subject and object must be processes.',
        'directed': True,
        'supports_passive_reversal': True,
        'polarity': 'negative',
    },
}


CLOSED_PREDICATES_BY_IRI = {entry['iri']: entry for entry in CLOSED_PREDICATES.values()}


def closed_predicate_whitelist_digest() -> str:
    return hashlib.sha256(
        json.dumps({'version': CLOSED_PREDICATE_SET_VERSION, 'predicates': CLOSED_PREDICATES},
                   sort_keys=True, ensure_ascii=False).encode('utf-8')
    ).hexdigest()


def compatible_closed_predicate_set(version: str | None, digest: str | None) -> bool:
    """Return True when a saved checkpoint matches current or legacy closed-relation whitelists."""
    return (
        version == CLOSED_PREDICATE_SET_VERSION and digest == closed_predicate_whitelist_digest()
    ) or (
        version in LEGACY_CLOSED_PREDICATE_SET_VERSIONS and digest in LEGACY_CLOSED_WHITELIST_DIGESTS
    )


def verify_closed_predicates(reference) -> dict[str, dict]:
    """Verify all closed-relation whitelist properties against imported RDF and effective types."""
    from backend.ontology.property_types import effective_property_restrictions

    allowed = set(reference.profile.get('allowed_object_properties', ()))
    verified = {}
    for alias, entry in CLOSED_PREDICATES.items():
        iri = entry['iri']
        term = reference.terms.get(iri)
        if not term or term.get('deprecated') or term.get('kind') != 'object_property':
            raise ValueError(f'Closed predicate {alias} ({iri}) is missing or deprecated in the pinned catalog')
        if iri not in allowed:
            raise ValueError(f'Closed predicate {alias} ({iri}) is not permitted in profile.allowed_object_properties')
        if 'exact' not in term.get('allowed_mappings', ('exact', 'broader', 'unresolved')):
            raise ValueError(f'Closed predicate {alias} ({iri}) is restricted from direct exact use')
        reqs = effective_property_restrictions(reference, iri)
        def _lbls(iris):
            return [reference.terms.get(i, {}).get('label', i.rsplit('/', 1)[-1]) for i in iris]
        verified[alias] = {
            **entry,
            'upstream_label': term['label'],
            'definition': term.get('definition') or '',
            'usage_notes': list(term.get('usage_notes') or ()),
            'comments': list(term.get('comments') or ()),
            'exact_synonyms': list(term.get('exact_synonyms') or ()),
            'related_synonyms': list(term.get('related_synonyms') or ()),
            'parent_iris': [p.get('iri') for p in term.get('parent_properties', ()) if p.get('iri')],
            'parent_labels': [p.get('label') for p in term.get('parent_properties', ()) if p.get('label')],
            'inverse_iris': list(term.get('inverse_of') or ()),
            'inverse_labels': _lbls(term.get('inverse_of') or ()),
            'effective_domain_iris': list(reqs['domain']),
            'effective_range_iris': list(reqs['range']),
            'effective_domain': _lbls(reqs['domain']),
            'effective_range': _lbls(reqs['range']),
        }
    return verified


@lru_cache(maxsize=4)
def _cached_glossary(signature: str, reference_dir: str) -> str:
    from backend.ontology.catalog import Catalog
    ref = Catalog(reference_dir)
    return _format_closed_predicate_glossary(verify_closed_predicates(ref))


def _format_closed_predicate_glossary(verified: dict[str, dict]) -> str:
    lines = [
        f'# CLOSED PREDICATE WHITELIST ({CLOSED_PREDICATE_SET_VERSION})',
        'Select predicate_alias from P1-P12 ONLY when the cited text and endpoint kinds satisfy the upstream definition, effective domain/range, and admission rule.',
        'Do NOT include unapproved catch-all or undirected effect properties; return {"claims": []} when no listed property applies faithfully.',
    ]
    for alias, meta in verified.items():
        dom = ', '.join(meta['effective_domain']) or 'unrestricted in fragment'
        rng = ', '.join(meta['effective_range']) or 'unrestricted in fragment'
        extras = []
        if meta['parent_labels']:
            extras.append('parents: ' + ', '.join(meta['parent_labels']))
        if meta['inverse_labels']:
            extras.append('inverseOf: ' + ', '.join(meta['inverse_labels']))
        if meta['usage_notes']:
            extras.append('notes: ' + '; '.join(meta['usage_notes']))
        if meta['comments']:
            extras.append('comments: ' + '; '.join(meta['comments']))
        if meta['exact_synonyms']:
            extras.append('exact synonyms: ' + ', '.join(meta['exact_synonyms']))
        if meta['related_synonyms']:
            extras.append('related wording (hints only): ' + ', '.join(meta['related_synonyms']))
        extra_text = (' | ' + ' | '.join(extras)) if extras else ''
        lines.append(
            f"- {alias} | {meta['display_label']} | {meta['id']} ({meta['upstream_label']})"
            f"\n  Definition: {meta['definition']}"
            f"\n  Effective domain (subject): {dom} | Effective range (object): {rng}{extra_text}"
            f"\n  Admission rule: {meta['admission_rule']}"
        )
    return '\n'.join(lines)


def render_closed_predicate_glossary(reference=None) -> str:
    if reference is None:
        from backend.ontology.catalog import catalog
        reference = catalog()
    if hasattr(reference, 'directory') and hasattr(reference, 'signature'):
        return _cached_glossary(reference.signature, str(reference.directory))
    return _format_closed_predicate_glossary(verify_closed_predicates(reference))


def valid_relation_wording(value: object) -> bool:
    """Require written predicate wording, not a one-character output artifact.

    This is a format check, not a relation whitelist or scientific verdict.
    """
    return isinstance(value, str) and len(value.strip()) >= 2


EXTRACTION_OUTPUT_TOKENS = 3000


COMPARATIVE_CUE_RE = re.compile(r"\b(?:compared with|compared to|versus|vs\.?|than|relative to)\b", re.IGNORECASE)


PURPOSE_ONLY_RE = re.compile(
    r"^\s*(?:use|use of|application|adoption|deployment)\b.+?\b(?:to|for)\s+"
    r"(?:increase|reduce|control|improve|maintain|avoid|prevent)\b", re.IGNORECASE)


FIGURE_OR_TABLE_RE = re.compile(r"^\s*(?:figure|table)\s*\d", re.IGNORECASE)


NON_EMPIRICAL_RE = re.compile(r"\b(?:no information on|no evidence (?:was )?(?:available|exists)|future research)\b", re.IGNORECASE)


ASSOCIATIVE_TRIGGER_RE = re.compile(
    r"\b(?:associated with|association between|correlated with|linked to|relationship between)\b",
    re.IGNORECASE,
)


EXTRACTION_RELATION_CUE_RE = re.compile(
    r"\b(?:affect\w*|influenc\w*|impact\w*|reduc\w*|decreas\w*|lower\w*|suppress\w*|"
    r"inhibit\w*|increas\w*|enhanc\w*|promot\w*|improv\w*|accumulat\w*|contain\w*|produc\w*|detect\w*|found|"
    r"contaminat\w*|associat\w*|correlat\w*|link(?:ed|s)?|higher|greater)\b",
    re.IGNORECASE,
)


EXTRACTION_PATHWAY_TERM_RE = re.compile(
    r"\b(?:soil|crop|plant|root|microb\w*|fung\w*|nutrient\w*|mineral\w*|contamin\w*|"
    r"pesticid\w*|toxin\w*|mycotoxin\w*|metal\w*|pathogen\w*|disease|exposure|intake|human|"
    r"health|phytochemical\w*|antioxidant\w*)\b",
    re.IGNORECASE,
)


CORE_PATHWAY_HINT_RE = re.compile(
    r'\b(?:nutrient\w*|nutrit\w*|contamin\w*|mycotoxin\w*|toxin\w*|exposure|intake|ingestion|'
    r'bioavailab\w*|food safety|human health)\b', re.IGNORECASE)


EXTRACTION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string", "minLength": 1, "description": "One source-worded entity, substance, process or trait; retain abbreviations and defining form, organ, state, deficiency, efficiency, ratio or event. Split independent outcomes, not joint causes or compound concepts. Put measurement values, units and times in qualifiers, not pure scientific endpoints; preserve grammatical number and defining restrictions."},
                    "object": {"type": "string", "minLength": 1, "description": "One source-worded entity, substance, process or trait, never a list of independently reported outcomes. Separate each outcome with its own metric. Retain defining form, organ, state, deficiency, efficiency, ratio or event. Preserve genuine joint treatments. Put measurement values, units and times in qualifiers, not pure scientific endpoints; preserve grammatical number and defining restrictions."},
                    # Avoid a regex here: the live route constrained \\S to one
                    # character during decoding despite JSON Schema search semantics.
                    "relation": {"type": "string", "minLength": 2,
                                 "description": "One standalone source-faithful predicate linking subject to object. For passive effects preserve causal roles: never change affected by to affects without reversing endpoints. A concessive background process is not necessarily the cause. Preserve inherited uncertainty in downstream clauses, direction, negation/modality/comparison (e.g. 'increases', 'may increase', 'does not affect', 'had higher'). Keep numbers in qualifiers.metric, cultivars/populations in qualifiers.population and comparisons in qualifiers.comparator. Preserve abilities as abilities. Do not substitute a population for a stated mechanism, compare a treatment code with a trait, or hide transported material in the predicate. Keep experimental destinations/settings in qualifiers.condition, not inside relation; never a single-letter code."},
                    "qualifiers": {
                        "type": "object",
                        "description": "Only crop_or_species, population, condition, comparator and metric. Keep population, dose, duration, setting, comparisons and measurements here without removing defining endpoint restrictions. Identify what context modifies. Omit unused keys; null is not an observation. No extra keys or invented values.",
                        "properties": {
                            "crop_or_species": {"type": ["string", "null"]},
                            "population": {"type": ["string", "null"]},
                            "condition": {"type": ["string", "null"], "description": "All applicable source-stated conditions and restrictions together in one string; preserve multiple details without adding subfields. Label treatment codes, doses, destinations and measurement times by their role; they are not resulting trait values."},
                            "comparator": {"type": ["string", "null"]},
                            "metric": {"type": ["string", "null"]},
                        },
                        "additionalProperties": False,
                    },
                    "measured_comparison": {
                        "type": "object", "additionalProperties": False,
                        "description": "For measured contrasts only. Subject names the study group or condition and object names the measured trait. Qualifiers carry baseline and measurement.",
                        "properties": {
                            "comparison_axis": {"type": "string", "enum": ["group", "condition"]},
                            "direction": {"type": "string", "enum": ["higher", "lower", "unchanged", "no_significant_difference"]},
                            "statistical_significance": {"type": "string", "enum": ["significant", "not_significant", "not_reported"]},
                            "comparator_quote": {"type": "string", "description": "Literal baseline wording from evidence, or empty when unspecified."},
                        },
                        "required": ["comparison_axis", "direction", "statistical_significance", "comparator_quote"],
                    },
                    "subject_anchor": {"type": "string", "description": "Exact span from cited evidence naming the subject; for a shared-word coordination include the entire supporting noun phrase, not just one conjunct. Cite any supplied antecedent sentence needed to name it."},
                    "object_anchor": {"type": "string", "description": "Exact span from cited evidence naming the object; for a shared-word coordination include the entire supporting noun phrase, not just one conjunct. Cite any supplied antecedent sentence needed to name it."},
                    "evidence_sentence_ids": {
                        "type": "array", "minItems": 1, "uniqueItems": True,
                        "items": {"type": "integer", "minimum": 1},
                    },
                    "subject_type": {"type": "string", "description": "Optional provisional hint; never a final ontology identity."},
                    "object_type": {"type": "string", "description": "Optional provisional hint; never a final ontology identity."},
                },
                "required": ["subject", "object", "relation", "qualifiers", "evidence_sentence_ids"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["claims"],
    "additionalProperties": False,
}


def closed_extraction_response_schema() -> dict:
    schema = deepcopy(EXTRACTION_RESPONSE_SCHEMA)
    item = schema['properties']['claims']['items']
    item['properties']['subject']['description'] = (
        'One atomic source-worded process, intervention, entity, or characteristic in the forward subject role of '
        'predicate_alias (process/intervention first for P1-P2, P8-P9, and P11-P12; cause/exposure/factor first for '
        'P3-P4 and P10; whole first for P7). '
        'Never use a bare organism/species or cultivar name as the subject of P1, P2, P11, or P12. Copy named '
        'treatment processes literally without reordering words; preserve genuine combined treatments; keep cultivars in qualifiers.population.'
    )
    item['properties']['object']['description'] = (
        'One atomic source-worded entity, process, condition, or characteristic in the forward object role of predicate_alias '
        '(characteristic for P1-P2; biological process for P11-P12; disease or abnormal condition for P10). '
        'Split coordinated traits/organs into separate claims with their own '
        'qualifiers.metric; preserve genuine single concepts such as root-to-shoot ratio.'
    )
    item['properties']['qualifiers']['description'] = (
        'Only crop_or_species, population, condition, comparator and metric. Keep the responsive group/cultivar in '
        'population and ALWAYS preserve any explicit relative comparison group stated on a causal or directional finding '
        '(e.g. "more prominently than Group H", "compared to Group H") in comparator without dropping it. '
        'Keep dose, duration, setting, value and unit here without stripping defining endpoint restrictions. '
        'For P5 copy reported sign and statistical significance into metric, together with any reported statistic.'
    )
    item['properties']['predicate_alias'] = {
        'type': 'string',
        'enum': list(CLOSED_PREDICATES),
        'description': 'Select the one approved object property whose full meaning and direction are asserted by the cited target clause.',
    }
    item['properties']['relation_anchor'] = {
        'type': 'string',
        'minLength': 1,
        'description': 'Copy the complete governing relation cue verbatim from a cited target sentence, including words that establish direction or qualify the assertion. Never use ellipses.',
    }
    item['properties']['subject_anchor']['description'] = (
        'Exact contiguous span from cited evidence naming the subject; copy named treatment processes literally and include '
        'the complete coordinated noun phrase when splitting a shared-word coordination.'
    )
    item['properties']['object_anchor']['description'] = (
        'Exact contiguous span from cited evidence naming the object; for a shared-word coordination include the complete '
        'contiguous coordinated noun phrase, and never insert ... ellipses.'
    )
    del item['properties']['relation']
    del item['properties']['measured_comparison']
    item['required'] = ['subject', 'object', 'predicate_alias', 'relation_anchor', 'qualifiers', 'evidence_sentence_ids']
    return schema


CLOSED_EXTRACTION_RESPONSE_SCHEMA = closed_extraction_response_schema()


def uses_entity_only_grounding(claim):
    return bool(claim.get('proposed_relation_iri') or (
        claim.get('measured_comparison') and claim.get('extraction_contract_version') == MEASURED_OBSERVATION_CONTRACT_VERSION
    ))
