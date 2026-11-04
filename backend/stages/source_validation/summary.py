"""Shared counts for source decisions, manual reviews, and assembly eligibility."""

from collections import Counter

from backend.stages.source_validation.review import source_fidelity_decision
from backend.stages.source_validation.scope import source_scope_counts


def source_assessment_counts(candidates):
    decisions = Counter(source_fidelity_decision(candidate) for candidate in candidates)
    return {
        **{f'source_{decision}': decisions[decision]
           for decision in ('entailed', 'unsupported', 'needs_review')},
        **source_scope_counts(candidates),
    }
