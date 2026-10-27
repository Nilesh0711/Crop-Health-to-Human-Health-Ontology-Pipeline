"""Experimental packet layouts over one immutable parsed sentence stream."""
from copy import deepcopy

from backend.shared.storage import stable_hash
from backend.stages.pdf_parsing.packets import iter_pdf_context_packets, word_count


def source_stream(pdf, inspection, page_start=1, page_end=None):
    """Parse once so both arms see exactly the same body text and chapter IDs."""
    rows = []
    for packet in iter_pdf_context_packets(pdf, inspection=inspection,
            max_pages=inspection['selected_pages'], packet_target_words=10**9, overlap_words=0):
        for sentence in packet['sentences']:
            if sentence['page'] < page_start or (page_end and sentence['page'] > page_end):
                continue
            row = deepcopy(sentence)
            row['source_sentence_id'] = stable_hash(
                f"{row.get('chapter_id')}|{row['page']}|{row.get('paragraph_id')}|{row.get('page_sentence_index')}|{row['text']}", 32)
            rows.append(row)
    return rows


def build_packets(rows, target_words=4000, overlap_words=500, *, preserve_paragraphs=False,
                  context_sentences=0, paragraph_slack_words=400):
    """Keep all source sentences; surrounding context is original quoted text.

    A very long paragraph may cross the target, bounded by paragraph_slack_words.
    Context expansion stays within the current heading/chapter, never guessed
    from a prior model answer. Two neighbors cannot resolve every distant referent.
    """
    if target_words < 1 or not 0 <= overlap_words < target_words:
        raise ValueError('Packet target must be positive and larger than overlap')
    if context_sentences < 0 or paragraph_slack_words < 0:
        raise ValueError('Context and paragraph slack must be nonnegative')
    packets, start = [], 0
    while start < len(rows):
        chapter = rows[start]['chapter_id']
        end, size = start, 0
        while end < len(rows) and rows[end]['chapter_id'] == chapter and (size < target_words or end == start):
            size += word_count(rows[end]['text'])
            end += 1
        if preserve_paragraphs:
            while (end < len(rows) and rows[end]['chapter_id'] == chapter
                   and rows[end].get('paragraph_id') and rows[end].get('paragraph_id') == rows[end-1].get('paragraph_id')
                   and size + word_count(rows[end]['text']) <= target_words + paragraph_slack_words):
                size += word_count(rows[end]['text'])
                end += 1
        left, right = start, end
        for _ in range(context_sentences):
            if (left > 0 and rows[left-1]['chapter_id'] == chapter
                    and rows[left-1]['section_heading'] == rows[start]['section_heading']):
                left -= 1
            if (right < len(rows) and rows[right]['chapter_id'] == chapter
                    and rows[right]['section_heading'] == rows[end-1]['section_heading']):
                right += 1
        selected = deepcopy(rows[left:right])
        headings = list(dict.fromkeys(r['section_heading'] for r in selected))
        words = sum(word_count(r['text']) for r in selected)
        packets.append({'section_heading': headings[0] if len(headings)==1 else f'{headings[0]} -> {headings[-1]}',
            'chapter_id': chapter, 'section_headings': headings, 'packet_number': len(packets)+1,
            'page_start': selected[0]['page'], 'page_end': selected[-1]['page'], 'word_count': words,
            'estimated_token_count': round(words*1.4), 'sentences': selected,
            'core_sentence_ids': [r['source_sentence_id'] for r in rows[start:end]],
            'context_extension_words': words-size,
            'core_starts_inside_paragraph': bool(start and rows[start].get('paragraph_id') == rows[start-1].get('paragraph_id')),
            'core_ends_inside_paragraph': bool(end<len(rows) and rows[end].get('paragraph_id') == rows[end-1].get('paragraph_id'))})
        if end == len(rows):
            break
        if rows[end]['chapter_id'] != chapter:
            start = end
            continue
        next_start, overlap = end, 0
        while next_start > start+1 and overlap < overlap_words:
            next_start -= 1
            overlap += word_count(rows[next_start]['text'])
        if preserve_paragraphs:
            while (next_start > start+1 and rows[next_start].get('paragraph_id')
                   and rows[next_start].get('paragraph_id') == rows[next_start-1].get('paragraph_id')
                   and overlap+word_count(rows[next_start-1]['text']) <= overlap_words+paragraph_slack_words):
                next_start -= 1
                overlap += word_count(rows[next_start]['text'])
        start = next_start
    return packets


def coverage_report(rows, packets):
    expected = {r['source_sentence_id'] for r in rows}
    received = {r['source_sentence_id'] for p in packets for r in p['sentences']}
    broken_chapters = sum(len({r['chapter_id'] for r in p['sentences']}) != 1 for p in packets)
    return {'sentences': len(expected), 'covered_sentences': len(expected & received),
            'missing_sentence_ids': sorted(expected-received), 'unknown_sentence_ids': sorted(received-expected),
            'chapter_crossings': broken_chapters,
            'paragraph_cuts': sum(p['core_starts_inside_paragraph']+p['core_ends_inside_paragraph'] for p in packets),
            'provided_words': sum(p['word_count'] for p in packets), 'packets':len(packets)}
