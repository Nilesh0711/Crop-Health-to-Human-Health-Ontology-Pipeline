"""Single source of truth for the bounded, domain-specific extraction prompt."""

from backend.config.paths import EXTRACTION_PROMPT
from backend.domain.ontology import RELATION_DEFINITIONS, ENTITY_DEFINITIONS, SCOPE_POLICY


def build_extraction_prompt(claims_per_packet: int, extraction_mode: str = 'whole_packet') -> str:
    template = EXTRACTION_PROMPT.read_text(encoding='utf-8').strip()
    if not template:
        raise ValueError('The extraction prompt template is empty')
    if extraction_mode == 'target_context':
        template += ('\nTARGET/CONTEXT CONTRACT: Only extraction_target supplies direct evidence for returned edges. '
                     'reading_context_only is supplied for interpretation, never as another extraction target. '
                     'All evidence_sentence_numbers must belong to the allowed target IDs, be unique and ascending (1-3 IDs). '
                     'Non-adjacent source fragments are allowed; do not hide intervening conditions or invent continuity. '
                     'Check every target sentence, including sentences with no eligible edge. An empty claims array '
                     'is valid. Keep endpoints atomic; do not join distinct outcomes to fit the claim limit. '
                     'Preserve restrictions resolved from the context; never use context to invent a missing edge.')
    vocabulary = '\n'.join(f'- {name}: {definition}' for name, definition in ENTITY_DEFINITIONS.items())
    limit = (f'Return at most {claims_per_packet} distinct, high-value edges. No target minimum.' if claims_per_packet
             else 'Return ALL eligible direct atomic edges in the assigned text. There is no numeric claim-count cap. '
                  'Do not omit a connected edge because other claims have already been extracted. Do not invent edges to increase count.')
    attention = ('Inspect only the explicitly marked extraction target.' if extraction_mode == 'target_context'
                 else 'Use the priority evidence index as an attention aid, then inspect the rest of the whole packet.')
    return (template + '\n\n' + limit + '\n' + attention + '\n'
            + 'Common relation names (examples, not a whitelist): ' + ', '.join(RELATION_DEFINITIONS)
            + '. Use another precise predicate when needed to preserve the source meaning; prefer concise snake_case. '
              'Causal, associative, comparative, production and adaptation edges are eligible when explicitly supported and in scope.'
            + '\nProvisional entity labels (external evidence assesses correctness; use Unresolved if no label fits):\n'
            + vocabulary + '\nScope policy:\n' + SCOPE_POLICY)
