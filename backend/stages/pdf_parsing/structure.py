"""Chapter boundaries, bibliography regions and observable PDF text diagnostics.

Bookmarks are preferred to text guesses. Without bookmarks, only explicit
chapter headings resume extraction after references; uncertainty is reported.
"""
import re
import unicodedata
from collections import Counter

from backend.shared.text_structure import REFERENCE_HEADING_TEXT

REFERENCES = re.compile(rf'^\s*(?:\d+(?:\.\d+)*\.?\s+)?{REFERENCE_HEADING_TEXT}\s*$', re.I)
CHAPTER = re.compile(r'^\s*chapter\s+(?:\d+|[IVX]+)\b', re.I)


def inspect_pdf(reader, max_pages=0):
    total = len(reader.pages)
    limit = min(total, max_pages) if max_pages else total
    texts, errors = [], {}
    for index in range(total):
        try:
            texts.append(unicodedata.normalize('NFKC', reader.pages[index].extract_text() or ''))
        except Exception as exc:
            texts.append('')
            errors[index + 1] = type(exc).__name__
    counts = Counter(line.strip() for text in texts for line in set(text.splitlines()) if line.strip())
    repeated = {line for line, count in counts.items() if count >= max(3, total // 5)
                and len(line) < 160 and (not line.endswith('.') or 'copyright' in line.lower())}
    boundaries = {}
    try:
        # Top-level destinations represent chapter/appendix boundaries; nested
        # subsection bookmarks must not terminate a chapter's bibliography.
        destinations = [d for d in reader.outline if not isinstance(d, list)]
        titles = [str(d.title) for d in destinations]
        book_outline = (any(CHAPTER.match(t) for t in titles) or
                        (any(re.fullmatch(r'front matter|(?:table of )?contents', t, re.I) for t in titles)
                         and sum(bool(re.match(r'^\d{1,2}\s+\w', t)) for t in titles) >= 2))
        if book_outline:
            for destination in destinations:
                page = reader.get_destination_page_number(destination) + 1
                title = str(destination.title)
                if page > 0 and not REFERENCES.fullmatch(title):
                    boundaries[page] = (title, 'pdf_bookmark')
    except (AttributeError, TypeError, ValueError, KeyError):
        pass
    cleaned = []
    for page, text in enumerate(texts, 1):
        lines = [line for line in text.splitlines() if line.strip() not in repeated]
        for line in lines[:12]:
            if CHAPTER.match(line) and len(line.split()) <= 20:
                boundaries.setdefault(page, (line.strip(), 'explicit_chapter_heading'))
                break
        cleaned.append('\n'.join(lines))
    regions, audits = [], []
    chapter_id, chapter_title, method = 'chapter-1', 'Document', 'document'
    chapter_start, in_references = 1, False
    for page, (raw, text) in enumerate(zip(texts, cleaned), 1):
        if page in boundaries:
            chapter_title, method = boundaries[page]
            chapter_id, chapter_start, in_references = f'chapter-{page}', page, False
        body, references = [], []
        for line in text.splitlines():
            if REFERENCES.fullmatch(line):
                in_references = True
                continue
            (references if in_references else body).append(line)
        regions.append({'page': page, 'chapter_id': chapter_id, 'chapter_title': chapter_title,
                        'chapter_start': chapter_start, 'boundary_method': method,
                        'body': '\n'.join(body), 'references': '\n'.join(references)})
        words = len(re.findall(r'\b\w+\b', text))
        flags = []
        if not raw.strip():
            flags.append('no_extracted_text_ocr_or_blank_page')
        elif words < 12:
            flags.append('sparse_text_possible_figure_or_scan')
        if '\ufffd' in raw:
            flags.append('replacement_characters')
        if len(re.findall(r'\b\w\s+\w\s+\w\b', text)) >= 3:
            flags.append('possible_reading_order_or_split_words')
        if page in errors:
            flags.append('extraction_error:' + errors[page])
        audits.append({'page': page, 'characters': len(raw), 'word_count': words,
                       'body_characters': len(regions[-1]['body']),
                       'reference_characters': len(regions[-1]['references']),
                       'within_page_limit': page <= limit, 'flags': flags,
                       'reading_order': 'suspect' if any('order' in f for f in flags) else 'not_verified',
                       'chapter_id': chapter_id})
    for region in regions:
        following = [p for p in boundaries if p > region['chapter_start']]
        region['chapter_end'] = min(following) - 1 if following else total
    return {'regions': regions, 'pages': audits, 'total_pages': total, 'selected_pages': limit,
            'pages_with_text': sum(bool(t.strip()) for t in texts),
            'pages_with_warnings': sum(bool(a['flags']) for a in audits),
            'text_extraction_coverage': sum(bool(t.strip()) for t in texts) / max(1, total),
            'coverage_basis': 'pages with extractable characters; not OCR accuracy',
            'chapter_boundaries': [{'page': p, 'title': title, 'method': method}
                                   for p, (title, method) in sorted(boundaries.items())],
            'warnings': ([] if boundaries else ['No chapter bookmarks/headings detected; bibliography continuation cannot be verified.'])}
