"""Model-assessed claim scope and admission to provisional pathway assembly.

These helpers read recorded decisions. They never classify scientific text,
entity names, relation labels, or keywords themselves.
"""
from backend.domain.ontology import PATHWAY_DESCRIPTION
from backend.stages.source_validation.review import source_fidelity_decision, source_review_state


PROJECT_SCOPE_SCHEMA = {
    'type': 'object',
    'properties': {
        'decision': {'type': 'string', 'enum': ['in_scope', 'out_of_scope', 'needs_review']},
        'rationale': {'type': 'string', 'minLength': 1},
    },
    'required': ['decision', 'rationale'],
    'additionalProperties': False,
}

PROJECT_SCOPE_INSTRUCTIONS = (
    '\nAlso assess PROJECT SCOPE independently from PDF fidelity in project_scope. '
    'Project domain: ' + PATHWAY_DESCRIPTION + ' '
    'Assess the exact atomic relationship, its qualifiers and supplied source context. '
    'Return in_scope for a source-context-grounded segment of this domain, out_of_scope '
    'for a clearly unrelated or purely economic, financial, market, infrastructure or '
    'aggregate-production assertion, and needs_review when relevance cannot be established. '
    'A paper being about agriculture does not make every assertion relevant. '
    'Do not exclude a biological or food-composition/quality/safety relationship merely '
    'because its surrounding passage also discusses costs or production. Conversely, '
    'do not invent a biological mechanism for an economic assertion. '
    'An upstream soil, crop, disease or food mechanism need not mention a human-health '
    'endpoint or complete the entire pathway. Numerical values alone neither establish '
    'nor exclude relevance; distinguish a measured biological or food-material property '
    'from a standalone productivity statistic. Judge the relationship in context, '
    'not a keyword list, relation whitelist, or the provisional entity-type labels. '
    'Do not invent missing downstream links to make a claim relevant. '
    'Give a brief rationale naming the actual relationship and why it fits or does '
    'not fit the project, or what context is missing. This is not external truth validation. '
    'A faithful out-of-scope assertion can have decision=entailed and '
    'project_scope.decision=out_of_scope. A relevant assertion can still be unsupported '
    'by the PDF. Scope must never change the fidelity verdict, rewrite an endpoint, '
    'reverse direction, drop negation or conditions, or upgrade source certainty. '
    'Do not alter the claim to force it into the project. '
)


def source_scope_decision(candidate):
    if (candidate.get('source_validation') or {}).get('manual_reviews'):
        return source_review_state(candidate)['scope_decision']
    scope = (candidate.get('source_validation') or {}).get('project_scope') or {}
    decision = scope.get('decision')
    return decision if decision in {'in_scope', 'out_of_scope', 'needs_review'} else 'not_assessed'


def source_pathway_eligible(candidate):
    return (not candidate.get('schema_error') and not candidate.get('superseded_by')
            and source_fidelity_decision(candidate) == 'entailed'
            and source_scope_decision(candidate) == 'in_scope')


def effective_project_scope(candidate):
    scope = dict((candidate.get('source_validation') or {}).get('project_scope') or {})
    scope['decision'] = source_scope_decision(candidate)
    reviews = (candidate.get('source_validation') or {}).get('manual_reviews')
    if reviews:
        review = source_review_state(candidate)['active_reviews'].get('project_scope')
        if review:
            scope.update(origin='manual', rationale=review.get('note') or f"Project scope manually {review['decision']}ed by the researcher.")
    return scope


def source_scope_counts(candidates):
    counts = dict(in_scope=0, out_of_scope=0, needs_review=0, not_assessed=0)
    for candidate in candidates:
        counts[source_scope_decision(candidate)] += 1
    return {'source_project_scope': counts,
            'pathway_eligible_claims': sum(source_pathway_eligible(c) for c in candidates)}
