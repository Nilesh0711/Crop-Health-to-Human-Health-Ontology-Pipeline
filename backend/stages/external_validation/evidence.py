"""Exact paper text and local sentence identifiers shared by active validators.

These utilities prepare stored text and reconstruct selected quotations. They do
not infer scientific support or make provider requests.
"""
from copy import deepcopy
import json

from backend.stages.literature_retrieval.clients import scholarly_sentences


def sentences(text):
    """Keep sentence numbering stable between provider input and proof checks."""
    return scholarly_sentences(str(text or ''))


def numbered_chunks(document):
    """Number all saved chunk sentences without deciding scientific relevance."""
    result = []
    for chunk in document.get('evidence_chunks', []):
        rows = [{'number': index, 'text': text} for index, text in enumerate(sentences(chunk.get('text')), 1)]
        if rows:
            result.append({'chunk_id': chunk.get('chunk_id'),
                           **{key: chunk[key] for key in ('source_kind', 'table_id', 'row_number', 'figure_id', 'layout_verified') if key in chunk},
                           'sentences': rows, 'priority_sentence_numbers': [],
                           'section_heading': chunk.get('section_heading', ''),
                           'paragraph_references': chunk.get('paragraph_references', [])})
    return result


def _pack_chunks(document, chunks):
    """Share exact repeated sentences within a paper without losing local IDs.

    All sentences, conditions, paragraph citations and chunk boundaries survive.
    Proof validation still uses the original saved chunks, never the compressed
    representation or another paper's sentence pool.
    """
    plain_chunks = deepcopy(chunks)
    pool, lookup = {}, {}
    for chunk in chunks:
        for row in chunk['sentences']:
            sentence = row.pop('text')
            if sentence not in lookup:
                identifier = f's{len(pool) + 1}'
                lookup[sentence] = identifier
                pool[identifier] = sentence
            row['sentence_id'] = lookup[sentence]
    metadata = {k: document.get(k) for k in ('evidence_document_id', 'title', 'doi', 'publication_year',
                    'evidence_level', 'evidence_access', 'source_reference_number', 'independence_group')}
    pooled = {**metadata, 'sentence_pool': pool, 'chunks': chunks}
    plain = {**metadata, 'chunks': plain_chunks}
    return min((plain, pooled), key=lambda value: len(json.dumps(value, ensure_ascii=False, separators=(',', ':'))))


def packed_document(document):
    """Pack every saved sentence; scientific relevance remains the judge's task."""
    return _pack_chunks(document, numbered_chunks(document))


def selected_context(document, selection, *, finding=False):
    """Reconstruct only real local IDs, keeping context gaps explicit."""
    chunk = next((c for c in document.get('evidence_chunks', []) if c.get('chunk_id') == selection.get('chunk_id')), None)
    numbers = selection.get('sentence_numbers')
    source_sentences = sentences((chunk or {}).get('text'))
    maximum = 3 if finding else 6
    if (not chunk or not isinstance(numbers, list) or not 1 <= len(numbers) <= maximum
            or any(type(n) is not int for n in numbers) or numbers != sorted(set(numbers))
            or not 1 <= numbers[0] <= numbers[-1] <= len(source_sentences)):
        return None, 'Selection must use unique ascending sentence IDs from this paper/chunk'
    fragments, group, previous = [], [], None
    for number in numbers:
        if previous is not None and number != previous + 1:
            fragments.append(' '.join(group)); group = []
        group.append(source_sentences[number - 1]); previous = number
    fragments.append(' '.join(group))
    if finding and sum(len(fragment.split()) for fragment in fragments) > 180:
        return None, 'A finding quotation exceeds 180 words; choose a shorter finding and separate context'
    gap = ' [NON-ADJACENT EVIDENCE] ' if finding else ' [NON-ADJACENT CONTEXT] '
    return {**selection, **{key: chunk[key] for key in ('source_kind', 'table_id', 'row_number', 'figure_id') if key in chunk},
            'fragments': fragments, 'is_contiguous': len(fragments) == 1,
            'intervening_context': [{'number': number, 'text': source_sentences[number - 1]}
                                    for number in range(numbers[0], numbers[-1] + 1) if number not in numbers] if finding else [],
            'quoted_span': gap.join(fragments)}, None
