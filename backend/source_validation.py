"""Check whether the originating PDF's cited text entails an extracted claim."""

from __future__ import annotations

import json
import re
import unicodedata
from typing import Any

from external_validator.claim_matching import entity_anchored
from external_validator.llm import OllamaClient
from external_validator.nli import NLIValidator, claim_hypothesis


_CAUSAL_RELATIONS = {"causes", "increases", "reduces", "leads_to", "deficiency_causes"}

# These are deliberately lexical rather than semantic synonyms.  They check
# that the extractor's *source trigger* is compatible with the graph relation,
# not that an arbitrary related phrase occurs somewhere in the evidence span.
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
    "required": ["decision", "relation_match", "direction_match", "certainty_match", "proof", "rationale"],
    "additionalProperties": False,
}


def _normalized_surface(value: object) -> str:
    """Normalise source text while retaining word order and phrase boundaries.

    PDF text often splits or joins source terms differently from the extractor:
    ``SoilTexture`` vs ``soil texture``, ``soil-texture``, and line-wrapped
    whitespace should all resolve to the same surface form.  This is *not* a
    synonym matcher; it deliberately cannot turn ``cadmium`` into ``lead``.
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

    A graph label such as ``SOC`` can safely stand for ``soil organic carbon``
    only when the same context packet contains ``soil organic carbon (SOC)``
    (or the reverse order).  This is deliberately much narrower than a
    synonym table: it cannot turn rice into wheat or one fertiliser into
    another.
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
    This additional check prevents Qwen from assigning that valid anchor to a
    different canonical entity (for example, a ``Rice`` claim anchored to a
    ``Wheat`` mention).  Formatting/plural differences remain safe through
    ``_mention_anchored``; explicitly defined abbreviations are the sole alias
    exception.
    """
    return (_mention_anchored(canonical, source_mention)
            or _explicit_abbreviation_equivalent(canonical, source_mention, context)
            or _explicit_abbreviation_equivalent(source_mention, canonical, context))


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
    """Return whether an anchored trigger does not contradict the relation.

    The former implementation treated a small lexical whitelist as a complete
    scientific predicate ontology.  That demoted faithful wording such as
    ``impact of``, ``positive driver``, or ``was shown to reduce`` merely
    because the exact phrase was absent from the dictionary.  Qwen already
    judges semantic equivalence; this final deterministic layer is therefore a
    safety check for explicit conflicts, not a second semantic classifier.
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
    packet = str(candidate.get("source_context_packet", ""))
    offset = packet.find(span)
    if offset < 0:
        return packet[:1200]
    return packet[max(0, offset - 600):offset] + " [EVIDENCE SPAN] " + packet[offset + len(span):offset + len(span) + 350]


def validate_source_claim(candidate: dict[str, Any], client: OllamaClient,
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

    system = (
        "Act as a skeptical source-evidence verifier. The extracted claim is an untrusted hypothesis that may be wrong. "
        "Judge whether the selected SOURCE PDF evidence span directly entails every part of that exact claim. "
        "Use the nearby context only to resolve an explicit abbreviation or pronoun; never use the paper title or world knowledge as proof. "
        "Check the exact subject, object, relation, direction, and source modality. This is an extraction-fidelity "
        "decision, not a global causal-truth decision: an observational directional result may be faithfully encoded as "
        "descriptive, while may/can/thought/suggested must be conditional, association/correlation must be associative, "
        "and causal is reserved for explicit causal wording. Preserve source modality through certainty_match; a mismatch "
        "means the extracted claim is not faithful enough for automatic acceptance. A mere association must not become "
        "causes, increases, reduces, or leads_to. "
        "Association does not prove causes, increases or reduces. A claim about a different crop, population or material is unsupported. "
        "When source surface mentions or a relation trigger are supplied, treat them as the required textual anchors and verify them "
        "against the selected evidence span; they do not by themselves prove the claim. "
        "Return JSON with decision (entailed, unsupported, needs_review), relation_match, direction_match, "
        "certainty_match, proof {subject, relation, object} containing exact substrings of the selected evidence span, "
        "and a brief rationale. Choose needs_review when wording is ambiguous."
    )
    payload = {
        "claim_hypothesis": {key: candidate.get(key) for key in ("subject", "relation", "object", "certainty", "qualifiers")},
        "source_surface_mentions": {
            "subject": subject_surface if has_subject_surface else None,
            "object": object_surface if has_object_surface else None,
        },
        "relation_trigger": relation_trigger if has_relation_trigger else None,
        "selected_evidence_span": span,
        "nearby_context": _nearby_context(candidate, span),
    }
    judged = client.chat_json(system, json.dumps(payload, ensure_ascii=False), max_tokens=520,
                              json_schema=_SOURCE_DECISION_SCHEMA)
    decision = str(judged.get("decision", "needs_review")).strip().lower()
    reasons: list[str] = []
    if decision not in {"entailed", "unsupported", "needs_review"}:
        decision = "needs_review"
        reasons.append("Invalid source decision")
    if decision == "entailed":
        for field in ("relation_match", "direction_match"):
            if judged.get(field) is not True:
                reasons.append(f"{field} was not confirmed")
        # Certainty is factual content, not a model-confidence score.  An
        # observed directional result is welcome, but it must be represented
        # as descriptive/conditional rather than upgraded to a causal claim.
        if judged.get("certainty_match") is not True:
            reasons.append("certainty_match was not confirmed; claim modality is stronger or different than the source")
        proof = _proof_from_judgment(judged)
        for field in ("subject", "object"):
            quoted = str(proof.get(field, "")).strip()
            if not quoted or not _surface_anchored(quoted, span):
                reasons.append(f"{field} proof is missing from the selected evidence")

        if not subject_surface:
            reasons.append("Subject source surface mention is missing")
        elif not _mention_anchored(subject_surface, span):
            reasons.append("Subject is not explicitly named in the selected evidence")
        elif not _canonical_surface_compatible(candidate.get("subject"), subject_surface,
                                               candidate.get("source_context_packet", "")):
            reasons.append("Claim subject is not safely aligned with its source surface mention")
        if not object_surface:
            reasons.append("Object source surface mention is missing")
        elif not _mention_anchored(object_surface, span):
            reasons.append("Object is not explicitly named in the selected evidence")
        elif not _canonical_surface_compatible(candidate.get("object"), object_surface,
                                               candidate.get("source_context_packet", "")):
            reasons.append("Claim object is not safely aligned with its source surface mention")

        # Upstream evidence-first extraction supplies a binding trigger.  Old
        # candidates use Qwen's quoted trigger as the conservative fallback.
        trigger_for_check = relation_trigger if has_relation_trigger else proof["relation"]
        if not trigger_for_check:
            reasons.append("Relation trigger is missing from the selected evidence")
        elif not _relation_trigger_compatible(candidate.get("relation"), trigger_for_check, span):
            reasons.append("Evidence relation trigger is not compatible with the claimed relation")
        if candidate.get("relation") in _CAUSAL_RELATIONS and _contains_association(trigger_for_check):
            reasons.append("Associative evidence does not establish a causal or directional effect")
        if reasons:
            decision = "needs_review"
    result = {**base, "decision": decision, "rationale": str(judged.get("rationale", "")).strip()[:400]}
    if reasons:
        result["reasons"] = reasons
    proof = _proof_from_judgment(judged)
    if any(proof.values()):
        result["proof"] = proof
    if nli_validator is not None:
        try:
            scores = nli_validator.score(span, claim_hypothesis(candidate))
            result["nli"] = {"model": nli_validator.settings.nli_model, "scores": scores}
            if result["decision"] == "entailed" and scores.get("contradiction", 0.0) >= 0.75:
                result["decision"] = "needs_review"
                result.setdefault("reasons", []).append("Independent NLI strongly contradicts the extracted claim")
        except Exception as exc:
            result["nli"] = {"model": nli_validator.settings.nli_model, "error": str(exc)[:240]}
    return result
