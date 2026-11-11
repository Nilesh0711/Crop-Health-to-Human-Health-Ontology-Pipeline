"""Single source of truth for the bounded, domain-specific extraction prompt."""

from backend.config.paths import EXTRACTION_PROMPT


def build_extraction_prompt(claims_per_packet: int, extraction_mode: str = 'whole_packet') -> str:
    template = EXTRACTION_PROMPT.read_text(encoding='utf-8').strip()
    if not template:
        raise ValueError('The extraction prompt template is empty')
    if extraction_mode == 'target_context':
        template += ('\nTARGET/CONTEXT CONTRACT: Extract only from extraction_target. '
                     'reading_context_only explains referents and conditions. Cite at least one target sentence; supplied reading-context IDs may support antecedents or restrictions, never a separate finding.')
    limit = (f'Return at most {claims_per_packet} distinct, high-value edges. No target minimum.' if claims_per_packet
             else 'Return ALL eligible direct atomic edges in the assigned text. There is no numeric claim-count cap. '
                  'Do not omit a connected edge because other claims have already been extracted. Do not invent edges to increase count.')
    attention = ('Inspect only the explicitly marked extraction target.' if extraction_mode == 'target_context'
                 else 'Use the priority evidence index as an attention aid, then inspect the rest of the whole packet.')
    return template + '\n\n' + limit + '\n' + attention
