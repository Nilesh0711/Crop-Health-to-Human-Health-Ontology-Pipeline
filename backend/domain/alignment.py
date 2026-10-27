"""Evidence-grounded semantic alignment shared by source and external checks."""

from __future__ import annotations

import re
import unicodedata
from typing import Any


ALIGNMENT_SCHEMA = {
    "type": "object",
    "properties": {
        "canonical": {"type": "string"},
        "mention": {"type": "string"},
        "kind": {"type": "string", "enum": ["same_concept", "coordination", "explicit_definition", "context_reference", "different"]},
        "evidence": {"type": "string"},
        "explanation": {"type": "string"},
    },
    "required": ["canonical", "mention", "kind", "evidence", "explanation"],
    "additionalProperties": False,
}

ENTITY_ALIGNMENTS_SCHEMA = {"type": "object", "properties": {
    "subject": ALIGNMENT_SCHEMA, "object": ALIGNMENT_SCHEMA},
    "required": ["subject", "object"], "additionalProperties": False}

ALIGNMENT_INSTRUCTIONS = (
    "Explain entity_alignment for each canonical endpoint: echo canonical, copy an exact mention and a short "
    "evidence substring, choose kind same_concept/coordination/explicit_definition/context_reference/different, "
    "and explain the mapping briefly. Equivalent scientific phrasing and a clearly distributed coordinated "
    "phrase may match. Availability, abundance, diversity, activity and content are distinct outcomes; "
    "an intervention improving function does not establish increased abundance. Do not substitute a different "
    "crop, chemical, material, intervention, or study outcome. A class label does not assert every member, "
    "but an increase in one subtype does not establish increased total abundance of its parent class. "
    "Class occurrence/containment can use an explicitly identified member; class-level quantitative changes "
    "require the same measured outcome. Never invent class membership or a definition from background knowledge. "
    "Resolve abbreviations and references only from the "
    "provided local context, with a quoted antecedent. For context_reference/explicit_definition, evidence must "
    "quote the actual supplied antecedent or section heading that names the canonical entity; merely quoting "
    "the shorter mention again does not establish its identity. All evidence and mentions must be verbatim "
    "contiguous text, never ellipses, slash-separated alternatives, or paraphrases. "
    "A treatment combination stays a combination unless "
    "the evidence independently attributes the effect to each component. "
)


def normalized_phrase(value: object) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return " ".join(re.findall(r"\w+", text))


def exact_in(value: object, text: object) -> bool:
    phrase = " ".join(str(value or "").split())
    return bool(phrase) and phrase.casefold() in " ".join(str(text or "").split()).casefold()


def grounded_alignment(alignment: object, canonical: object, decisive_text: str,
                       context: str = "") -> bool:
    """A semantic decision must identify the same target and cite real text.

    This records model reasoning rather than encoding domain synonym maps.
    Context can bind a referent but cannot supply a missing result relation.
    Legacy responses without these fields retain their lexical safeguards.
    """
    if not isinstance(alignment, dict) or normalized_phrase(alignment.get("canonical")) != normalized_phrase(canonical):
        return False
    mention, evidence = alignment.get("mention"), alignment.get("evidence")
    kind = alignment.get("kind")
    if kind not in {"same_concept", "coordination", "explicit_definition", "context_reference"}:
        return False
    if not str(alignment.get("explanation") or "").strip() or not exact_in(mention, decisive_text):
        return False
    if kind in {"same_concept", "coordination"}:
        return exact_in(evidence, decisive_text) and exact_in(mention, evidence)
    if kind == 'context_reference':
        # An antecedent and its anaphor normally occur in different sentences.
        # The mention was already grounded in decisive_text above; require the
        # separately quoted antecedent to name the canonical entity in the
        # supplied local context. The model owns the coreference judgment.
        return exact_in(evidence, context) and exact_in(canonical, evidence)
    return (exact_in(evidence, context) and exact_in(canonical, evidence)
            and exact_in(mention, evidence))
