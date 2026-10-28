"""Check whether the originating PDF's cited text entails an extracted claim."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from backend.domain.modality import uses_conditional_modality
from backend.domain.claim_matching import entity_anchored
from backend.domain.alignment import ALIGNMENT_INSTRUCTIONS, ENTITY_ALIGNMENTS_SCHEMA, exact_in, normalized_phrase
from backend.providers.llm import OpenCodeClient
from backend.providers.nli import NLIValidator, claim_hypothesis
from backend.stages.source_validation.abbreviations import (
    ABBREVIATION_SCHEMA, ABBREVIATION_INSTRUCTIONS, SOURCE_VALIDATION_POLICY,
    prepare_name_expansions, commit_name_expansions,
)


# These lexical patterns supply attention notes only. They cannot accept,
# reject, rewrite or override a source model judgement.
_RELATION_TRIGGER_PATTERNS: dict[str, tuple[re.Pattern[str], ...]] = {
    "affects": (
        re.compile(r"\baffect\w*\b"), re.compile(r"\binfluenc\w*\b"),
        re.compile(r"\bimpact\w*\b"), re.compile(r"\balter\w*\b"),
        re.compile(r"\bchange\w*\b"), re.compile(r"\bmodulat\w*\b"),
        re.compile(r"\bregulat\w*\b"), re.compile(r"\bdetermin\w*\b"),
    ),
    "causes": (
        re.compile(r"\bcaus(?:e|es|ed|ing)?\b"), re.compile(r"\binduc\w*\b"),
        re.compile(r"\btrigger\w*\b"), re.compile(r"\bproduc\w*\b"),
        re.compile(r"\bresult(?:s|ed|ing)? in\b"), re.compile(r"\blead(?:s|ing)? to\b"),
        re.compile(r"\bled to\b"),
    ),
    "increases": (
        re.compile(r"\bincreas\w*\b"), re.compile(r"\belevat\w*\b"),
        re.compile(r"\benhanc\w*\b"), re.compile(r"\brais\w*\b"),
        re.compile(r"\bpromot\w*\b"), re.compile(r"\bboost\w*\b"),
        re.compile(r"\baugment\w*\b"), re.compile(r"\bbolster\w*\b"),
        re.compile(r"\bhigher\b"),
        re.compile(r"\bgreater\b"), re.compile(r"\bmore\b"),
    ),
    "reduces": (
        re.compile(r"\breduc\w*\b"), re.compile(r"\bdecreas\w*\b"),
        re.compile(r"\blower\w*\b"), re.compile(r"\bsuppress\w*\b"),
        re.compile(r"\binhibit\w*\b"), re.compile(r"\bdeclin\w*\b"),
        re.compile(r"\bdiminish\w*\b"), re.compile(r"\battenuat\w*\b"),
        re.compile(r"\bimpair\w*\b"), re.compile(r"\bless\b"),
        re.compile(r"\bprotect\w* against\b"),
    ),
    "leads_to": (
        re.compile(r"\blead(?:s|ing)? to\b"), re.compile(r"\bled to\b"),
        re.compile(r"\bresult(?:s|ed|ing)? in\b"), re.compile(r"\bcaus(?:e|es|ed|ing)?\b"),
        re.compile(r"\bproduc\w*\b"), re.compile(r"\btrigger\w*\b"),
    ),
    "associated_with": (
        re.compile(r"\bassociat\w* (?:with|between)\b"),
        re.compile(r"\bcorrelat\w* (?:with|between)\b"),
        re.compile(r"\blink(?:ed|age)? (?:to|with)\b"),
        re.compile(r"\brelat\w* (?:to|with)\b"),
        re.compile(r"\brelationship between\b"),
    ),
    "has_nutrient": (
        re.compile(r"\bcontain\w*\b"), re.compile(r"\brich in\b"),
        re.compile(r"\bsource of\b"), re.compile(r"\bprovid\w*\b"),
        re.compile(r"\bcontent of\b"), re.compile(r"\bconcentrat\w* of\b"),
        re.compile(r"\blevels? of\b"),
    ),
    "has_contaminant": (
        re.compile(r"\bcontain\w*\b"), re.compile(r"\bcontaminat\w*\b"),
        re.compile(r"\bdetect\w*\b"), re.compile(r"\baccumulat\w*\b"),
        re.compile(r"\bresidu\w*\b"), re.compile(r"\bpresence of\b"),
        re.compile(r"\bfound in\b"), re.compile(r"\boccur\w*\b"),
        re.compile(r"\bconcentrat\w* of\b"), re.compile(r"\bcontent of\b"),
    ),
    "contains": (
        re.compile(r"\bcontain\w*\b"), re.compile(r"\bcompris\w*\b"),
        re.compile(r"\binclude\w*\b"), re.compile(r"\bcontent of\b"),
        re.compile(r"\bconcentrat\w* of\b"), re.compile(r"\bpresence of\b"),
    ),
    "has_trait": (
        re.compile(r"\bhas\b"), re.compile(r"\bhave\b"), re.compile(r"\bhad\b"),
        re.compile(r"\bwith\b"), re.compile(r"\bexhibit\w*\b"),
        re.compile(r"\bcharacteri[sz]\w* by\b"), re.compile(r"\bshow\w*\b"),
    ),
    "higher_than": (
        re.compile(r"\bhigher\b"), re.compile(r"\bgreater\b"),
        re.compile(r"\bmore\b"), re.compile(r"\bexceed\w*\b"),
    ),
    "lower_than": (
        re.compile(r"\blower\b"), re.compile(r"\bless\b"),
        re.compile(r"\bfewer\b"), re.compile(r"\bbelow\b"),
    ),
}
_ASSOCIATION_ONLY = re.compile(
    r"\b(?:associat\w* (?:with|between)|correlat\w* (?:with|between)|"
    r"link(?:ed|age)? (?:to|with)|relat\w* (?:to|with)|relationship between)\b",
    re.IGNORECASE,
)
_DEFICIENCY_CUE = re.compile(r"\b(?:deficien\w*|insufficien\w*|deplet\w*)\b")

_SOURCE_DECISION_SCHEMA = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["entailed", "unsupported", "needs_review"]},
        "relation_match": {"type": "boolean"},
        "direction_match": {"type": "boolean"},
        "certainty_match": {"type": "boolean"},
        "claim_text_match": {"type": "boolean"},
        "occurrence_provenance_fields": {"type": "array", "maxItems": 2, "items": {
            "type": "object", "properties": {
                "field": {"type": "string", "enum": ["condition", "population"]},
                "value": {"type": "string"}, "evidence": {"type": "string"}, "rationale": {"type": "string"}},
            "required": ["field", "value", "evidence", "rationale"], "additionalProperties": False}},
        "entity_alignment": ENTITY_ALIGNMENTS_SCHEMA,
        "abbreviation_review": ABBREVIATION_SCHEMA,
        "proof": {
            "type": "object",
            "properties": {
                "subject": {"type": "string"},
                "relation": {"type": "string"},
                "object": {"type": "string"},
            },
            "required": ["subject", "relation", "object"],
            "additionalProperties": False,
        },
        "rationale": {"type": "string"},
    },
    "required": ["decision", "relation_match", "direction_match", "certainty_match", "claim_text_match", "occurrence_provenance_fields", "entity_alignment", "abbreviation_review", "proof", "rationale"],
    "additionalProperties": False,
}


def _check_source_response_complete(response):
    """A positive verdict must actually supply proof; do not judge its truth here."""
    if response.get('decision') == 'entailed':
        from backend.providers.structured_output import IncompleteResponseError
        proof = response.get('proof') or {}
        if any(not str(proof.get(role) or '').strip() for role in ('subject', 'relation', 'object')):
            raise IncompleteResponseError('An entailed source response omitted required proof wording')
    review = response.get('abbreviation_review')
    if isinstance(review, dict):
        status, corrections, unresolved = (review.get(k) for k in ('status', 'corrections', 'unresolved'))
        if ((status == 'clear' and (corrections or unresolved))
                or (status == 'expanded' and (not corrections or unresolved))
                or (status == 'unresolved' and not unresolved)):
            from backend.providers.structured_output import IncompleteResponseError
            raise IncompleteResponseError('Source abbreviation review is internally inconsistent')


def _normalized_surface(value: object) -> str:
    """Normalise source text while retaining word order and phrase boundaries.

    PDF text often splits or joins source terms differently from the extractor:
    ``SoilTexture`` vs ``soil texture``, ``soil-texture``, and line-wrapped
    whitespace should all resolve to the same surface form.  This is *not* a
    synonym matcher; it deliberately cannot turn one chemical into another.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    # Split lower-to-upper and acronym-to-word boundaries before case folding:
    # SoilTexture -> Soil Texture, DNARepair -> DNA Repair.
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text)
    text = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", text)
    text = text.replace("_", " ").replace("/", " ")
    return " ".join(re.findall(r"[a-z0-9]+", text.casefold()))


def _surface_anchored(value: object, evidence: object) -> bool:
    """Return whether a normalised phrase occurs contiguously in evidence."""
    phrase = _normalized_surface(value).split()
    text = _normalized_surface(evidence).split()
    if not phrase or len(phrase) > len(text):
        return False
    return any(text[index:index + len(phrase)] == phrase for index in range(len(text) - len(phrase) + 1))


def _mention_anchored(value: object, evidence: object) -> bool:
    """Check an exact/normalised surface form, then the legacy plural-aware form."""
    text = str(value or "").strip()
    return bool(text) and (_surface_anchored(text, evidence) or entity_anchored(text, str(evidence or "")))


def _source_mention(candidate: dict[str, Any], role: str) -> tuple[str, bool]:
    """Get a source surface mention, preferring the extractor's exact mention.

    The boolean distinguishes an explicit-but-empty upstream field from a
    legacy candidate, where the canonical graph entity is the only fallback.
    """
    key = f"{role}_source_mention"
    if key in candidate:
        return str(candidate.get(key) or "").strip(), True
    return str(candidate.get(role) or "").strip(), False


def _explicit_abbreviation_equivalent(canonical: object, source_mention: object, context: object) -> bool:
    """Allow an alias only when the local PDF explicitly defines it.

    An initialism may stand for a full name only when the same context packet
    explicitly defines both forms together. This is deliberately narrower than
    a synonym table: it cannot replace one crop or treatment with another.
    """
    short = str(canonical or "").strip()
    long = str(source_mention or "").strip()
    if not re.fullmatch(r"[A-Z][A-Z0-9-]{1,9}", short) or not long:
        return False
    source_context = str(context or "")
    if not source_context:
        return False
    escaped_short = re.escape(short)
    escaped_long = re.escape(long)
    return bool(
        re.search(rf"(?i){escaped_long}\s*\(\s*{escaped_short}\s*\)", source_context)
        or re.search(rf"(?i)\b{escaped_short}\s*\(\s*{escaped_long}\s*\)", source_context)
    )


def _canonical_surface_compatible(canonical: object, source_mention: object, context: object) -> bool:
    """Require the graph entity to be present in its named source surface.

    The extraction-stage anchor proves only that *some* phrase is in the PDF.
    This additional check prevents the evidence model from assigning that valid anchor to a
    different canonical entity. Formatting/plural differences remain safe
    through ``_mention_anchored``. Explicitly defined abbreviations and shared
    domain modifiers are the only lexical exceptions. Model-grounded identity
    is checked separately.
    """
    canonical_terms = _normalized_surface(canonical).split()
    source_terms = _normalized_surface(source_mention).split()
    domain_modifiers = {"soil", "crop", "plant", "human"}
    canonical_core = [term for term in canonical_terms if term not in domain_modifiers]
    source_core = [term for term in source_terms if term not in domain_modifiers]
    modifier_only_variant = bool(canonical_core) and canonical_core == source_core
    return (_mention_anchored(canonical, source_mention)
            or modifier_only_variant
            or _explicit_abbreviation_equivalent(canonical, source_mention, context)
            or _explicit_abbreviation_equivalent(source_mention, canonical, context))


_CONTEXTUAL_TOPIC_MENTION = re.compile(
    r"\b(?:the\s+)?(?:disease|disease intensity|infection|pathogen|pest|condition)\b",
    re.IGNORECASE,
)


def _contextual_topic_compatible(candidate: dict[str, Any], canonical: object,
                                 source_mention: object) -> bool:
    """Allow a generic local mention only when an exact section heading binds it."""
    heading = str(candidate.get("source_evidence_section_heading")
                  or candidate.get("source_section_heading") or "").strip()
    mention = str(source_mention or "").strip()
    if not heading or not mention or len(_normalized_surface(mention).split()) > 7:
        return False
    return _mention_anchored(canonical, heading) and bool(_CONTEXTUAL_TOPIC_MENTION.search(mention))


def _proof_from_judgment(judged: dict[str, Any]) -> dict[str, str]:
    """Read current proof JSON while retaining compatibility with old records."""
    raw = judged.get("proof")
    proof = raw if isinstance(raw, dict) else {}
    return {
        "subject": str(proof.get("subject", judged.get("subject_quote", "")) or "").strip(),
        "relation": str(proof.get("relation", judged.get("relation_quote", "")) or "").strip(),
        "object": str(proof.get("object", judged.get("object_quote", "")) or "").strip(),
    }


def _relation_trigger_compatible(relation: object, trigger: object, evidence: object) -> bool:
    """Return a lexical hint only; never override the source model's decision.

    The former implementation treated a small lexical whitelist as a complete
    scientific predicate ontology.  That demoted faithful wording such as
    ``impact of``, ``positive driver``, or ``was shown to reduce`` merely
    because the exact phrase was absent from the dictionary. The evidence model already
    judges semantic equivalence; this helper only records an attention note.
    """
    relation_name = str(relation or "").strip()
    trigger_text = str(trigger or "").strip()
    if not trigger_text or not _surface_anchored(trigger_text, evidence):
        return False

    normalised_trigger = _normalized_surface(trigger_text)
    if relation_name == "deficiency_causes":
        causal_patterns = (*_RELATION_TRIGGER_PATTERNS["causes"], *_RELATION_TRIGGER_PATTERNS["leads_to"])
        return bool(_DEFICIENCY_CUE.search(_normalized_surface(evidence)) and
                    any(pattern.search(normalised_trigger) for pattern in causal_patterns))

    if relation_name in {"causes", "increases", "reduces", "leads_to"} and _contains_association(trigger_text):
        return False

    increasing = any(pattern.search(normalised_trigger) for pattern in _RELATION_TRIGGER_PATTERNS["increases"])
    reducing = any(pattern.search(normalised_trigger) for pattern in _RELATION_TRIGGER_PATTERNS["reduces"])
    if relation_name in {"increases", "higher_than"} and reducing and not increasing:
        return False
    if relation_name in {"reduces", "lower_than"} and increasing and not reducing:
        return False

    # A known positive cue is useful, but unknown wording is left to the
    # evidence-bound semantic judgement instead of being rejected.
    patterns = _RELATION_TRIGGER_PATTERNS.get(relation_name, ())
    return not patterns or any(pattern.search(normalised_trigger) for pattern in patterns) or bool(normalised_trigger)


def _contains_association(trigger: object) -> bool:
    return bool(_ASSOCIATION_ONLY.search(_normalized_surface(trigger)))


def _nearby_context(candidate: dict[str, Any], span: str) -> str:
    if candidate.get('source_section_context'):
        return str(candidate['source_section_context'])
    if candidate.get("source_local_context"):
        return str(candidate["source_local_context"])
    packet = str(candidate.get("source_context_packet", ""))
    heading = str(candidate.get("source_evidence_section_heading")
                  or candidate.get("source_section_heading") or "").strip()
    heading_prefix = f"[LOCAL SECTION HEADING] {heading}. " if heading else ""
    offset = packet.find(span)
    if offset < 0:
        return heading_prefix + packet[:1200]
    return (heading_prefix + packet[max(0, offset - 600):offset] + " [EVIDENCE SPAN] "
            + packet[offset + len(span):offset + len(span) + 350])


def _source_alignment_grounded(mapping, canonical, proof_texts, context):
    """Verify source provenance and response consistency, not scientific synonyms.

    The source model owns identity/coreference. A composite canonical name need
    not occur literally in its defining heading. Mentions must occur in selected
    proof; definitions/context must quote text actually supplied to that model.
    """
    if (not isinstance(mapping, dict)
            or normalized_phrase(mapping.get('canonical')) != normalized_phrase(canonical)
            or mapping.get('kind') not in {'same_concept', 'coordination', 'explicit_definition', 'context_reference'}
            or not str(mapping.get('explanation') or '').strip()
            or not any(exact_in(mapping.get('mention'), text) for text in proof_texts)):
        return False
    evidence = mapping.get('evidence')
    if mapping['kind'] in {'same_concept', 'coordination'}:
        return any(exact_in(evidence, text) for text in proof_texts) and exact_in(mapping['mention'], evidence)
    return exact_in(evidence, context) or any(exact_in(evidence, text) for text in proof_texts)


def validate_source_claim(candidate: dict[str, Any], client: OpenCodeClient,
                          nli_validator: NLIValidator | None = None) -> dict[str, Any]:
    span = str(candidate.get("source_evidence_span", "")).strip()
    numbers = candidate.get("source_evidence_sentence_numbers", [])
    base = {"candidate_id": candidate["candidate_id"]}
    if not span or not isinstance(numbers, list) or not 1 <= len(numbers) <= 3:
        return {**base, "decision": "unsupported", "rationale": "Missing source evidence."}

    subject_surface, has_subject_surface = _source_mention(candidate, "subject")
    object_surface, has_object_surface = _source_mention(candidate, "object")
    has_relation_trigger = "relation_trigger" in candidate
    relation_trigger = str(candidate.get("relation_trigger") or "").strip()
    proof_texts = [span]
    if candidate.get('source_evidence_is_contiguous') is False:
        proof_texts = [f['text'] for f in candidate.get('source_evidence_fragments', [])
                       if isinstance(f, dict) and isinstance(f.get('text'), str)]

    system = (
        "Act as a skeptical source-evidence verifier. The extracted claim is an untrusted hypothesis that may be wrong. "
        "Judge whether the selected SOURCE PDF evidence span directly entails every part of that exact claim. "
        "Use the supplied same-section context only to resolve an explicit abbreviation, pronoun, contextual term "
        "such as 'then', or generic topic mention that is bound by the supplied "
        "local section heading (abstract example: 'disease intensity' inside a 'Disease D of Crop C' section). The context includes "
        "original sentence IDs/pages. Use actual earlier definitions/antecedents when the nearest sentences are insufficient. "
        "Never use the paper title, "
        "a distant paragraph, or world knowledge as proof. The selected span must still state the object and relation. "
        "Check the exact subject, object, relation, direction, and source modality. Relation names are open; "
        "assess their meaning against the source, not a predicate whitelist or a type-pair rule. "
        "Extraction review hints are unverified attention notes, not evidence. This is an extraction-fidelity "
        "decision, not a global causal-truth decision: an observational directional result may be faithfully encoded as "
        "descriptive, while may/can/thought/suggested must be conditional, association/correlation must be associative, "
        "and causal is reserved for explicit causal wording. Preserve source modality through certainty_match; a mismatch "
        "means the extracted claim is not faithful enough for automatic acceptance. A bare association without "
        "direction must not become causes, increases, reduces, or leads_to. "
        "Judge one atomic edge only. If X affects an intermediate M and M affects Y (including a relative clause such as "
        "'M, which increases Y'), the passage does not entail a collapsed X-to-Y edge. "
        "An explicitly stated paired directional trend (X decreases as Y increases) may be encoded as Y reduces X "
        "with descriptive certainty and a non-causal claim_text. It does not prove causation. A bare correlation without "
        "a stated direction cannot support increases/reduces. A claim about a different crop, population or material is unsupported. "
        "Abstract fidelity examples (placeholder entities, never evidence): 'Factor A changes Process B, "
        "which changes Outcome C' does NOT entail a direct Factor A->Outcome C edge. Its two edges are "
        "Factor A->Process B and Process B->Outcome C, even when claim_text mentions both. "
        "'X may affect Y' cannot validate certainty=causal. 'X directly affects Y' alone cannot validate "
        "relation=reduces. 'X threatens Y' is a threat/risk, not a measured reduction of Y. "
        "Use respectively/list-item assignments exactly; do not assign one listed property to all items. "
        "If the evidence explicitly restricts the finding to a named crop, population, dose, condition, comparator, or exposure route "
        "and the claim omits that restriction from both its entities and qualifiers, choose needs_review. Empty qualifier fields are "
        "otherwise normal; do not demand information that the selected evidence does not state. "
        "For a crop has_contaminant occurrence claim, a location or farming setting stored as source_occurrence_context "
        "describes where the source observed it; the atomic crop-contaminant edge does not assert universal prevalence. "
        "Do not demand that setting again as a population qualifier. Keep actual species, dose, treatment, disease "
        "condition, or exposed-human-population restrictions in qualifiers. "
        "Classify misplaced occurrence provenance in occurrence_provenance_fields: ONLY for descriptive has_contaminant, "
        "identify any condition/population field that describes merely the geographic site or commercial/subsistence farming "
        "setting of a reported occurrence, not an actual biological restriction. Echo its exact field/value and quote a real "
        "substring from the selected evidence explaining that setting. Otherwise return an empty list. Never classify infection, "
        "genotype, dose, processing/treatment, comparator or exposed-human subgroup as mere provenance; keep uncertain cases "
        "as restrictions. This is a role classification, not permission to invent a broader causal or prevalence claim. "
        "For example, condition='Disease D infection' in an affected Crop C material claim is a biological "
        "restriction and must remain, even when the disease is also named in a section heading. "
        "When source surface mentions or a relation trigger are supplied, treat them as the required textual anchors and verify them "
        "against the selected evidence span; they do not by themselves prove the claim. "
        + ALIGNMENT_INSTRUCTIONS +
        "Use the shortest sufficient literal proof anchors; do not recopy whole OCR-damaged sentences. "
        "proof.subject and proof.object must contain or equal their corresponding entity_alignment.mention; "
        "do not put one endpoint's mention in the proof slot for a different endpoint. The full selected evidence must "
        "still support the property. Canonical entities can combine a local referent with a heading/definition: "
        "in a 'Disease D affecting Crop C material' section, 'this material' can refer to Disease D-affected Crop C "
        "material when the supplied context establishes that identity. Quote 'this material' as the mention and the actual supplied heading/antecedent as binding evidence. "
        "The complete combined canonical phrase need not appear literally in that heading. Explain the binding; "
        "a heading does not permit replacing a material with a disease or one crop with another. "
        "Preserve contrasts: 'Factor A changes Outcome X; Factor B changes Outcome Y but not Outcome X' "
        "does not support a Factor B->changes->Outcome X edge. Bind each effect and negation to its actual subject. "
        "Also check claim_text_match: if a contextual claim_text is provided, every assertion, qualifier and modality "
        "in that restatement must be supported by this same evidence and explicit local referents. Extra inferred "
        "mechanisms, health consequences, causal upgrades, or changed measured outcomes make it false. "
        "A faithful paraphrase need not be verbatim; require exact substrings for proof anchors only. "
        "Read compressed causal and explanatory constructions in their full sentence context. "
        "An implicitly expressed relationship is not automatically a reversed relationship. "
        "Identify which endpoints a causal connector actually joins; if more than one reading is plausible, "
        "choose needs_review and describe the ambiguity instead of inventing an opposite direction. "
        "Numerical values must describe the same measured outcome, unit and denominator as the extracted edge. "
        "Do not reject neutral reporting language alone, but 'samples' requires an explicit sampling setting. "
        "A neutral structured_rendering is just the structured claim repeated, not additional evidence. "
        "Return JSON with decision (entailed, unsupported, needs_review), relation_match, direction_match, "
        "certainty_match, proof {subject, relation, object} containing exact substrings of the selected evidence span, "
        "and a brief rationale. Choose needs_review when wording is ambiguous."
    )
    system += ABBREVIATION_INSTRUCTIONS
    system += (
        ' Source evidence may contain explicitly marked non-adjacent fragments. '
        'source_evidence_fragments preserves their original sentence IDs/pages; source_intervening_context '
        'contains EVERY omitted sentence between them, including tables or conflicting findings. '
        'A gap marker is application metadata, never source wording. Do not treat separate fragments '
        'as one continuous quotation or infer a missing causal link. Check intervening text for conditions '
        'and contradictions; choose needs_review if continuity or the direct edge cannot be established. '
        'A table or running page header interrupting a source sentence is not by itself a scientific break: '
        'sentence fragments can continue across that interruption if their actual text establishes continuity. '
        'Assess continuity from the supplied text, not the existence of a gap alone. '
        'Proof anchors must each occur literally within an actual selected fragment, not across a gap.')
    payload = {
        'source_evidence_fragments': candidate.get('source_evidence_fragments', []),
        'source_intervening_context': candidate.get('source_intervening_context', []),
        "claim_hypothesis": {key: candidate.get(key) for key in ("subject", "relation", "object", "certainty", "qualifiers")},
        "claim_text": candidate.get("claim_text"),
        "claim_text_origin": candidate.get("claim_text_origin"),
        "canonical_name_fields": {key: candidate.get(key) for key in ('subject', 'object', 'relation', 'claim_text', 'source_context', 'qualifiers')},
        "extraction_review_hints": candidate.get("extraction_review_hints", []),
        "source_occurrence_context": candidate.get("source_context") or None,
        "source_surface_mentions": {
            "subject": subject_surface if has_subject_surface else None,
            "object": object_surface if has_object_surface else None,
        },
        "relation_trigger": relation_trigger if has_relation_trigger else None,
        "selected_evidence_span": span,
        "nearby_context": _nearby_context(candidate, span),
        **({'source_reassessment': candidate['_source_reassessment']} if candidate.get('_source_reassessment') else {}),
    }
    def check_response(response):
        _check_source_response_complete(response)
        if response.get('decision') != 'entailed':
            return
        from backend.providers.structured_output import EvidenceSelectionError
        proof = _proof_from_judgment(response)
        errors = [f'{role} proof must quote an actual selected fragment' for role in ('subject', 'relation', 'object')
                  if not any(_surface_anchored(proof[role], text) for text in proof_texts)]
        for role, mapping in (response.get('entity_alignment') or {}).items():
            if role in ('subject', 'object') and isinstance(mapping, dict):
                mention = mapping.get('mention')
                if not (exact_in(mention, proof[role]) or exact_in(proof[role], mention)):
                    errors.append(f'{role} proof must use the same endpoint mention as its alignment')
        if errors:
            raise EvidenceSelectionError('; '.join(errors))

    judged = client.chat_json(system, json.dumps(payload, ensure_ascii=False), max_tokens=1600,
                              json_schema=_SOURCE_DECISION_SCHEMA, request_label=f"source: {candidate['candidate_id']}",
                              response_check=check_response)
    original_candidate = candidate
    abbreviation_review = judged.get('abbreviation_review')
    candidate, name_errors = prepare_name_expansions(candidate, abbreviation_review,
                                                    _nearby_context(candidate, span) + ' ' + span)
    alignments = judged.get("entity_alignment") if isinstance(judged.get("entity_alignment"), dict) else {}
    grounded = {role: _source_alignment_grounded(alignments.get(role), candidate.get(role), proof_texts,
                                               _nearby_context(candidate, span))
                for role in ("subject", "object")}
    decision = str(judged.get("decision", "needs_review")).strip().lower()
    reasons: list[str] = list(name_errors)
    if decision not in {"entailed", "unsupported", "needs_review"}:
        decision = "needs_review"
        reasons.append("Invalid source decision")
    if decision == "entailed":
        if candidate.get("claim_text_origin") == "extracted" and judged.get("claim_text_match") is not True:
            reasons.append("Contextual claim text was not confirmed faithful to the source")
        required_model_fields = ("relation_match", "direction_match")
        for field in required_model_fields:
            if judged.get(field) is not True:
                reasons.append(f"{field} was not confirmed")
        # Certainty is factual content, not a model-confidence score.  An
        # observed directional result is welcome, but it must be represented
        # as descriptive/conditional rather than upgraded to a causal claim.
        if judged.get("certainty_match") is not True:
            reasons.append("certainty_match was not confirmed; claim modality is stronger or different than the source")
        proof = _proof_from_judgment(judged)
        for role in alignments:
            if role in grounded and not grounded[role]:
                reasons.append(f"{role} semantic alignment is not grounded in the selected evidence")
        for field in ("subject", "object", "relation"):
            quoted = str(proof.get(field, "")).strip()
            if not quoted or not any(_surface_anchored(quoted, text) for text in proof_texts):
                reasons.append(f"{field} proof is missing from the selected evidence")
        for role, mapping in alignments.items():
            if role in ('subject', 'object') and isinstance(mapping, dict):
                mention = mapping.get('mention')
                if not (exact_in(mention, proof[role]) or exact_in(proof[role], mention)):
                    reasons.append(f'{role} proof and entity alignment select inconsistent endpoint mentions')

        if not subject_surface:
            reasons.append("Subject source surface mention is missing")
        elif not any(_mention_anchored(subject_surface, text) for text in proof_texts):
            reasons.append("Subject is not explicitly named in the selected evidence")
        elif not (_canonical_surface_compatible(candidate.get("subject"), subject_surface,
                                                candidate.get("source_context_packet", ""))
                  or _contextual_topic_compatible(candidate, candidate.get("subject"), subject_surface)
                  or grounded["subject"]):
            reasons.append("Claim subject is not safely aligned with its source surface mention")
        if not object_surface:
            reasons.append("Object source surface mention is missing")
        elif not any(_mention_anchored(object_surface, text) for text in proof_texts):
            reasons.append("Object is not explicitly named in the selected evidence")
        elif not (_canonical_surface_compatible(candidate.get("object"), object_surface,
                                                candidate.get("source_context_packet", ""))
                  or _contextual_topic_compatible(candidate, candidate.get("object"), object_surface)
                  or grounded["object"]):
            reasons.append("Claim object is not safely aligned with its source surface mention")

        # Upstream evidence-first extraction supplies a binding trigger.  Old
        # candidates use the evidence model's quoted trigger as the conservative fallback.
        trigger_for_check = relation_trigger if has_relation_trigger else proof["relation"]
        if not trigger_for_check:
            reasons.append("Relation trigger is missing from the selected evidence")
        elif not any(_surface_anchored(trigger_for_check, text) for text in proof_texts):
            reasons.append("Relation trigger is missing from the selected evidence")
    result = {**base, "decision": decision, 'model_decision': judged.get('decision'),
              'model_matches': {k: judged.get(k) for k in ('relation_match', 'direction_match', 'certainty_match')},
              "rationale": str(judged.get("rationale", "")).strip()[:400],
              'validation_policy': SOURCE_VALIDATION_POLICY}
    if isinstance(abbreviation_review, dict):
        result['abbreviation_review'] = abbreviation_review
    if candidate.get("claim_text"):
        result["claim_text_match"] = judged.get("claim_text_match") is True
    if isinstance(judged.get('occurrence_provenance_fields'), list):
        result['occurrence_provenance_fields'] = judged['occurrence_provenance_fields']
    if isinstance(abbreviation_review, dict):
        noops = [edit for edit in abbreviation_review.get('corrections', []) if edit.get('before') == edit.get('after')]
        if noops:
            result['unchanged_name_annotations'] = noops
    if alignments:
        result["entity_alignment"] = alignments
    trigger = relation_trigger if has_relation_trigger else _proof_from_judgment(judged)["relation"]
    review_hints = []
    if trigger and _surface_anchored(trigger, span) and not _relation_trigger_compatible(candidate.get('relation'), trigger, span):
        review_hints.append('review_relation_wording')
    if uses_conditional_modality(span, trigger) and candidate.get('certainty') != 'conditional':
        review_hints.append('review_source_modality')
    if review_hints:
        result['review_hints'] = review_hints
    if reasons:
        result["reasons"] = reasons
        if decision == "entailed":
            decision = "needs_review"
            result["decision"] = decision
    proof = _proof_from_judgment(judged)
    if any(proof.values()):
        result["proof"] = proof
    if nli_validator is not None:
        try:
            heading = str(candidate.get("source_evidence_section_heading")
                          or candidate.get("source_section_heading") or "").strip()
            contextual_binding = (
                _contextual_topic_compatible(candidate, candidate.get("subject"), subject_surface)
                or _contextual_topic_compatible(candidate, candidate.get("object"), object_surface)
            )
            premise = f"Section: {heading}. {span}" if contextual_binding and heading else span
            scores = nli_validator.score(premise, claim_hypothesis(candidate))
            result["nli"] = {"model": nli_validator.settings.nli_model, "scores": scores}
            if result["decision"] == "entailed" and scores.get("contradiction", 0.0) >= 0.75:
                result["decision"] = "needs_review"
                result.setdefault("reasons", []).append("Independent NLI strongly contradicts the extracted claim")
        except Exception as exc:
            result["nli"] = {"model": nli_validator.settings.nli_model, "error": str(exc)[:240]}
    if result['decision'] == 'entailed':
        applied = commit_name_expansions(original_candidate, candidate, abbreviation_review)
        if applied:
            result['applied_name_corrections'] = applied
    return result
