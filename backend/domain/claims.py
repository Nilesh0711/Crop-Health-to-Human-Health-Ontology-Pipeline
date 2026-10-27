"""Generate a fixed claim description for retrieval without adding scientific evidence."""

from __future__ import annotations

from typing import Any


def generate_hypothesis(candidate: dict[str, Any]) -> str:
    """Build a stable retrieval sentence from the claim itself.

    The prior model-generated HyDE passage cost a provider request per claim
    and could add unverified concepts. The bi-encoder and cross-encoder only
    need the fixed endpoints, relation, and nonempty restrictions.
    """
    qualifiers = "; ".join(f"{key.replace('_', ' ')}: {value}" for key, value in
                           (candidate.get("qualifiers") or {}).items() if value)
    claim = (f"{candidate.get('subject', '')} "
             f"{str(candidate.get('relation') or '').replace('_', ' ')} "
             f"{candidate.get('object', '')}.")
    description = str(candidate.get('claim_text') or '') if (candidate.get('source_validation') or {}).get('claim_text_match') is True else ''
    return f"{claim} {qualifiers} {description}".strip()
