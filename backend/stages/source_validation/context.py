"""Keep actual same-section PDF sentences available for source reference binding."""


def section_context(candidate, packet):
    rows = packet.get('sentences') or []
    numbers = candidate.get('source_evidence_sentence_numbers') or []
    if not numbers or any(type(n) is not int or not 1 <= n <= len(rows) for n in numbers):
        return ''
    index = numbers[0] - 1
    heading = candidate.get('source_evidence_section_heading') or rows[index].get('section_heading')
    chapter = rows[index].get('chapter_id')
    if not heading or rows[index].get('section_heading') != heading:
        return ''
    def same_section(row):
        return row.get('section_heading') == heading and row.get('chapter_id') == chapter
    start, end = index, index + 1
    while start and same_section(rows[start - 1]):
        start -= 1
    while end < len(rows) and same_section(rows[end]):
        end += 1
    return '[LOCAL SECTION HEADING] ' + heading + '\n' + '\n'.join(
        f"[{n + 1}, PDF page {rows[n].get('page')}] {rows[n]['text']}" for n in range(start, end))
