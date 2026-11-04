"""Assess originating-PDF fidelity and project scope as separate model decisions."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from backend.domain.modality import uses_conditional_modality
from backend.domain.claim_matching import entity_anchored
from backend.domain.alignment import ENTITY_ALIGNMENTS_SCHEMA, exact_in, normalized_phrase
from backend.stages.source_validation.prompt import SOURCE_SYSTEM
from backend.providers.llm import OpenCodeClient
from backend.stages.source_validation.scope import PROJECT_SCOPE_SCHEMA
from backend.stages.source_validation.output import compact_source_schema, bind_source_output
from backend.stages.source_validation.abbreviations import (
    ABBREVIATION_SCHEMA, SOURCE_VALIDATION_POLICY,
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

_SOURCE_DECISION_SCHEMA = compact_source_schema({
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["entailed", "unsupported", "needs_review"]},
        "project_scope": PROJECT_SCOPE_SCHEMA,
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
    "required": ["decision", "project_scope", "relation_match", "direction_match", "certainty_match", "claim_text_match", "occurrence_provenance_fields", "entity_alignment", "abbreviation_review", "proof", "rationale"],
    "additionalProperties": False,
})


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
    evidence = mapping.get('evidence') or mapping.get('mention')
    if mapping['kind'] in {'same_concept', 'coordination'}:
        return any(exact_in(evidence, text) for text in proof_texts) and exact_in(mapping['mention'], evidence)
    return exact_in(evidence, context) or any(exact_in(evidence, text) for text in proof_texts)


def validate_source_claim(candidate: dict[str, Any], client: OpenCodeClient) -> dict[str, Any]:
    span = str(candidate.get("source_evidence_span", "")).strip()
    numbers = candidate.get("source_evidence_sentence_numbers", [])
    base = {"candidate_id": candidate["candidate_id"]}
    if not span or not isinstance(numbers, list) or not 1 <= len(numbers) <= 3:
        return {**base, "decision": "unsupported", "rationale": "Missing source evidence.",
                'project_scope': {'decision': 'needs_review', 'rationale': 'Missing source evidence; scope was not assessed.'},
                'validation_policy': SOURCE_VALIDATION_POLICY}

    subject_surface, has_subject_surface = _source_mention(candidate, "subject")
    object_surface, has_object_surface = _source_mention(candidate, "object")
    has_relation_trigger = "relation_trigger" in candidate
    relation_trigger = str(candidate.get("relation_trigger") or "").strip()
    proof_texts = [span]
    if candidate.get('source_evidence_is_contiguous') is False:
        proof_texts = [f['text'] for f in candidate.get('source_evidence_fragments', [])
                       if isinstance(f, dict) and isinstance(f.get('text'), str)]

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
    }
    def check_response(response):
        response = bind_source_output(candidate, response)
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

    judged = client.chat_json(SOURCE_SYSTEM, json.dumps(payload, ensure_ascii=False), max_tokens=1800,
                              json_schema=_SOURCE_DECISION_SCHEMA, request_label=f"source: {candidate['candidate_id']}",
                              response_check=check_response)
    judged = bind_source_output(candidate, judged)
    original_candidate = candidate
    abbreviation_review = judged.get('abbreviation_review')
    candidate, name_errors = prepare_name_expansions(candidate, abbreviation_review,
                                                    _nearby_context(candidate, span) + ' ' + span)
    alignments = judged.get("entity_alignment") if isinstance(judged.get("entity_alignment"), dict) else {}
    for role in ('subject', 'object'):
        if isinstance(alignments.get(role), dict):
            alignments[role].setdefault('canonical', candidate.get(role, ''))
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
              'project_scope': judged.get('project_scope') or {
                  'decision': 'needs_review', 'rationale': 'No model project-scope decision was recorded.'},
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
    if result['decision'] == 'entailed':
        applied = commit_name_expansions(original_candidate, candidate, abbreviation_review)
        if applied:
            result['applied_name_corrections'] = applied
    return result
