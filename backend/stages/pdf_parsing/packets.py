"""Parse chapter-scoped bibliography links and sentence-preserving PDF context packets."""

from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path
from typing import Any
from pypdf import PdfReader
from backend.stages.pdf_parsing.structure import inspect_pdf
from backend.shared.text import SPACE_RE, normalized, word_count
from backend.shared.storage import stable_hash
from backend.stages.pdf_parsing.constants import (
    APA_AUTHOR_YEAR_REFERENCE_RE,
    AUTHOR_YEAR_CITATION_RE,
    AUTHOR_YEAR_REFERENCE_RE,
    BARE_AUTHOR_YEAR_CITATION_RE,
    CITATION_GROUP_RE,
    DOI_RE,
    ESTIMATED_TOKENS_PER_WORD,
    EXTRACTION_PACKET_OVERLAP_WORDS,
    EXTRACTION_PACKET_TARGET_WORDS,
    HEADING_INITIAL_LIST_VERBS,
    HEADING_TRAILING_FRAGMENTS,
    JOURNAL_HEADER_RE,
    PARENTHETICAL_BLOCK_RE,
    PARENTHETICAL_SINGLE_AUTHOR_YEAR_RE,
    PMCID_RE,
    PMID_RE,
    PUBLISHER_BOILERPLATE_RE,
    REFERENCE_ENTRY_RE,
    REFERENCE_HEADING_RE,
    SCHOLARLY_ABBREVIATION_RE,
    SECTION_HEADING_RE,
    SENTENCE_BOUNDARY_RE,
    SENTENCE_PERIOD_MARKER,
    UNDOTTED_SECTION_STEMS,
)


def _looks_like_index_listing(text: str) -> bool:
    """Detect extracted contents/figure lists that resemble long sentences."""
    return (len(re.findall(r"\b\d{1,2}-\d{1,3}\b", text)) >= 3
            or len(re.findall(r",\s*\d{1,3}\b", text)) >= 6)


def source_paper_title(pdf_path: Path) -> str:
    """Prefer embedded PDF metadata while retaining a stable filename fallback."""
    metadata = PdfReader(str(pdf_path)).metadata
    title = str(metadata.title or "").strip() if metadata else ""
    return title if 12 <= len(title) <= 250 else pdf_path.stem


def source_article_identifiers(pdf_path: Path) -> dict[str, str]:
    """Read only reliable source-paper identifiers embedded in the local PDF.

    These identifiers are provenance, not world knowledge.  They let external
    retrieval reject the originating article even when Europe PMC uses a
    slightly different title or the local filename is only a fallback title.
    A value is kept only when it is unambiguous in PDF metadata or front
    matter, so a DOI from a cited reference cannot accidentally exclude an
    independent article.
    """
    empty = {"source_doi": "", "source_pmcid": "", "source_pmid": ""}
    try:
        reader = PdfReader(str(pdf_path))
        metadata = reader.metadata
        metadata_text = " ".join(str(value) for value in (metadata or {}).values())
        first_page_text = reader.pages[0].extract_text() or "" if reader.pages else ""
    except Exception:
        return empty

    def values(pattern: re.Pattern[str], text: str, *, group: int = 0) -> list[str]:
        found = [match.group(group).strip().rstrip(".,;:") for match in pattern.finditer(text)]
        return list(dict.fromkeys(item for item in found if item))

    def choose(metadata_values: list[str], page_values: list[str]) -> str:
        if len(metadata_values) == 1:
            return metadata_values[0]
        if len(page_values) == 1:
            return page_values[0]
        return ""

    return {
        "source_doi": choose(values(DOI_RE, metadata_text), values(DOI_RE, first_page_text)).casefold(),
        "source_pmcid": choose(values(PMCID_RE, metadata_text), values(PMCID_RE, first_page_text)).upper(),
        "source_pmid": choose(values(PMID_RE, metadata_text, group=1), values(PMID_RE, first_page_text, group=1)),
    }


def _citation_author_key(value: object) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return "".join(character for character in text.casefold() if character.isalnum())


def pdf_bibliography(pdf_path: Path, inspection=None) -> dict[int | str, dict[str, Any]]:
    """Parse numbered and author-year bibliography entries for citation-first retrieval.

    The full reference text is provenance only.  It is not treated as support
    until an independently retrieved article passage passes entailment.
    """
    try:
        inspection = inspection if inspection is not None else inspect_pdf(PdfReader(str(pdf_path)))
    except Exception:
        return {}
    chapters = {}
    for region in inspection['regions']:
        chapters.setdefault(region['chapter_id'], []).append(region)
    combined = {}
    aliases = {}
    for chapter, regions in chapters.items():
        reference_text = '\n'.join(r['references'] for r in regions)
        for key, record in _parse_bibliography_entries(reference_text).items():
            record = {**record, 'chapter_id': chapter, 'chapter_title': regions[0]['chapter_title'],
                      'source_page_start': regions[0]['chapter_start'], 'source_page_end': regions[0]['chapter_end']}
            combined[f'{chapter}::{key}'] = record
            aliases.setdefault(key, []).append(record)
    # An unscoped alias is safe only when unique across chapter bibliographies.
    for key, records in aliases.items():
        if len(records) == 1:
            combined[key] = records[0]
    return combined


def _parse_bibliography_entries(reference_text):
    entries: dict[int | str, dict[str, Any]] = {}
    ambiguous_keys: set[int | str] = set()
    for entry in REFERENCE_ENTRY_RE.finditer(reference_text):
        number = int(entry.group("number"))
        citation = SPACE_RE.sub(" ", entry.group("text")).strip()
        if not citation:
            continue
        if number in entries:
            if entries[number]['citation_text'] != citation[:2000]:
                ambiguous_keys.add(number)
            continue
        doi_match = DOI_RE.search(citation)
        entries[number] = {
            "reference_number": number,
            "citation_text": citation[:2000],
            "doi": doi_match.group(0).rstrip(".,;:)").casefold() if doi_match else "",
        }

    # Many agronomy journals use author-year references rather than numbered
    # citations. Segment those entries from one detected first-author/year line
    # to the next and expose a stable key such as ``zhang|2022d``. The entry is
    # still only a retrieval lead; it cannot count as evidence by itself.
    # Support both ``Surname, ... 2022.`` and APA ``Surname, A. (2022).``
    # layouts. Deduplicate matches that start at the same bibliography entry.
    detected_entries = [*AUTHOR_YEAR_REFERENCE_RE.finditer(reference_text),
                        *APA_AUTHOR_YEAR_REFERENCE_RE.finditer(reference_text)]
    author_entries = []
    seen_entry_starts: set[int] = set()
    for entry in sorted(detected_entries, key=lambda item: item.start()):
        if entry.start() not in seen_entry_starts:
            author_entries.append(entry)
            seen_entry_starts.add(entry.start())
    base_year_keys: dict[str, list[str]] = {}
    for index, entry in enumerate(author_entries):
        end = author_entries[index + 1].start() if index + 1 < len(author_entries) else len(reference_text)
        citation = SPACE_RE.sub(" ", reference_text[entry.start():end]).strip()
        author = _citation_author_key(entry.group("author"))
        year = entry.group("year").casefold()
        if not citation or not author:
            continue
        key = f"{author}|{year}"
        doi_match = DOI_RE.search(citation)
        record = {
            "reference_number": f"{entry.group('author')} {entry.group('year')}",
            "citation_key": key,
            "citation_text": citation[:2000],
            "doi": doi_match.group(0).rstrip(".,;:)").casefold() if doi_match else "",
        }
        if key in entries and entries[key]['citation_text'] != record['citation_text']:
            ambiguous_keys.add(key)
        entries.setdefault(key, record)
        base_year_keys.setdefault(f"{author}|{year[:4]}", []).append(key)
    # A citation sometimes omits the a/b/c suffix. Add the suffix-free alias
    # only when it resolves to exactly one bibliography entry.
    for base_key, full_keys in base_year_keys.items():
        if len(set(full_keys)) == 1 and full_keys[0] not in ambiguous_keys:
            entries.setdefault(base_key, entries[full_keys[0]])
    for key in ambiguous_keys:
        entries.pop(key, None)
    return entries


def citation_numbers(text: str) -> list[int]:
    """Return explicit bracketed reference numbers, expanding numeric ranges."""
    numbers: set[int] = set()
    for match in CITATION_GROUP_RE.finditer(str(text or "")):
        value = match.group("numbers")
        for part in re.split(r"\s*[,;]\s*", value):
            endpoints = re.split(r"\s*[-\u2013\u2014\ufffd]\s*", part)
            try:
                start = int(endpoints[0])
                end = int(endpoints[-1])
            except (ValueError, IndexError):
                continue
            if len(endpoints) > 1 and start <= end <= start + 50:
                numbers.update(range(start, end + 1))
            else:
                numbers.add(start)
    return sorted(numbers)


def author_year_citation_keys(text: str) -> list[str]:
    """Return stable first-author/year keys from local author-year citations."""
    keys: set[str] = set()
    value = str(text or "")
    matches = [*AUTHOR_YEAR_CITATION_RE.finditer(value), *BARE_AUTHOR_YEAR_CITATION_RE.finditer(value)]
    for block in PARENTHETICAL_BLOCK_RE.finditer(value):
        matches.extend(PARENTHETICAL_SINGLE_AUTHOR_YEAR_RE.finditer(block.group("content")))
    for match in matches:
        author = _citation_author_key(match.group("author"))
        year = match.group("year").casefold()
        if author:
            keys.add(f"{author}|{year}")
    return sorted(keys)


def link_source_citations(bibliography, span, page, chapter=''):
    references, links, seen = [], [], set()
    for key in [*citation_numbers(span), *author_year_citation_keys(span)]:
        reference = bibliography.get(f'{chapter}::{key}') or bibliography.get(key)
        if reference and not reference.get('source_page_start', 1) <= page <= reference.get('source_page_end', 10**9):
            reference = None
        links.append({'citation_key': str(key), 'chapter_id': chapter, 'page': page,
                      'status': 'identified' if reference else 'missing_bibliography_link', 'reference': reference})
        if reference:
            identity = json.dumps(reference, sort_keys=True)
            if identity not in seen:
                seen.add(identity)
                references.append(reference)
    return references, links


def page_sentences(text: str) -> list[str]:
    """Produce readable sentence units from extracted PDF text without a new NLP dependency."""
    # Keep list items separate: a PDF bullet list can contain several unrelated
    # findings while looking like one sentence after whitespace is collapsed.
    repaired = re.sub(r"(?<=\w)-\s*\n\s*(?=[a-z])", "", text)
    repaired = SCHOLARLY_ABBREVIATION_RE.sub(
        lambda match: match.group(0).replace(".", SENTENCE_PERIOD_MARKER), repaired
    )
    sentences: list[str] = []
    for item in re.split(r"\s*[•●▪]\s*", repaired):
        compact = SPACE_RE.sub(" ", item).strip()
        if compact:
            sentences.extend(
                sentence.replace(SENTENCE_PERIOD_MARKER, ".").strip()
                for sentence in SENTENCE_BOUNDARY_RE.split(compact) if sentence.strip()
            )
    return sentences


def _section_name(line: str) -> str | None:
    """Return a numbered research-paper section heading, when present."""
    compact = SPACE_RE.sub(" ", line).strip()
    match = SECTION_HEADING_RE.match(compact)
    if not match or int(match.group("number").split(".", 1)[0]) > 20:
        return None
    title = match.group("title").strip()
    words = re.findall(r"[A-Za-z]+", title.casefold())
    # Tables and enumerated prose often look like ``5. Stability of`` or
    # ``1. Provide ...`` after PDF extraction. They are not section context.
    if (not words or words[-1] in HEADING_TRAILING_FRAGMENTS
            or words[0] in HEADING_INITIAL_LIST_VERBS
            or re.search(r"(?:\d+|[-–])\s*$", title)):
        return None
    # A final dot (``2. Methods``) is strong section syntax.  Without one,
    # require a research-heading stem: PDFs frequently number author
    # affiliations such as ``2 Department of ...`` on their title page.
    if match.group("terminal_dot") or title.split()[0].casefold() in UNDOTTED_SECTION_STEMS:
        return compact
    return None


def _unnumbered_topic_heading(line: str) -> str | None:
    """Recognize explicit short title/subtitle syntax without topic/place lists.

    Numbered headings and PDF typography handle other heading styles. A body
    line cannot become a heading merely because it names a disease or region.
    """
    compact = SPACE_RE.sub(" ", line).strip()
    if not 4 <= len(compact) <= 120 or compact.endswith((".", ";", "?", "!")):
        return None
    words = re.findall(r"[A-Za-z]+", compact)
    if not 2 <= len(words) <= 18 or not compact[0].isupper():
        return None
    colon_heading = (":" in compact and not compact.endswith(":")
                     and not re.search(r"[\[\](){}]", compact)
                     and bool(re.search(r":\s*[A-Za-z].+", compact)))
    return compact if colon_heading else None


def _append_section_sentences(section: dict[str, Any], paragraph: str, page: int, seen: set[str]) -> None:
    paragraph_id = stable_hash(f"{page}|{section['heading']}|{normalized(paragraph)}", 20)
    for sentence_index, sentence in enumerate(page_sentences(paragraph), 1):
        sentence_key = normalized(sentence)
        if (len(re.findall(r"\w+", sentence)) < 6 or sentence_key in seen
                or PUBLISHER_BOILERPLATE_RE.search(sentence) or _looks_like_index_listing(sentence)):
            continue
        seen.add(sentence_key)
        section["sentences"].append({"page": page, "page_sentence_index": sentence_index,
                                     "paragraph_id": paragraph_id, "text": sentence})


def layout_heading_candidates(page: Any, extracted_text: str) -> set[str]:
    """Recognize short bold standalone headings from PDF typography, not disease names.

    Combine font runs on the same extracted line before checking its style: a
    punctuation glyph in a different font must not split a heading. This covers
    unnumbered topics without disease, crop, location or paper-name lists.
    """
    lines = {SPACE_RE.sub(" ", line).strip() for line in extracted_text.splitlines()}
    headings: set[str] = set()
    fragments: list[str] = []
    letter_styles: list[bool] = []
    punctuation_font_change: list[bool] = []

    def eligible(line: str) -> bool:
        return (line in lines and 2 <= len(line.split()) <= 18 and len(line) <= 140
                and line[:1].isupper() and not line.endswith((".", ";", "?", "!"))
                and not re.match(r"^(?:figure|fig\.|table)\b", line, re.IGNORECASE)
                and not PUBLISHER_BOILERPLATE_RE.search(line))

    def finish_line() -> None:
        line = SPACE_RE.sub(" ", unicodedata.normalize("NFKC", ''.join(fragments))).strip()
        if (letter_styles and all(letter_styles) and punctuation_font_change and eligible(line)
                and not re.search(r'\d|[&=<>]', line)):
            headings.add(line)
        fragments.clear()
        letter_styles.clear()
        punctuation_font_change.clear()

    def visit(text: str, _cm: Any, _tm: Any, font: Any, _size: float) -> None:
        name = str((font or {}).get("/BaseFont", ""))
        bold = bool(re.search(r"bold|[.+_-]b$", name, re.IGNORECASE))
        for fragment in text.splitlines(keepends=True):
            line = SPACE_RE.sub(" ", unicodedata.normalize("NFKC", fragment)).strip()
            if bold and eligible(line):
                headings.add(line)
            fragments.append(fragment.rstrip('\r\n'))
            letter_styles.extend(bold for character in fragment if character.isalnum())
            # Reconstruct typography around quotation marks/dashes only, not
            # mixed-font table expressions, author markers or formulae.
            glyphs = [c for c in fragment if not c.isspace()]
            if not bold and glyphs and all(c in "'\"" or unicodedata.category(c) in {'Pi', 'Pf', 'Pd'} for c in glyphs):
                punctuation_font_change.append(True)
            if fragment.endswith(('\n', '\r')):
                finish_line()

    try:
        page.extract_text(visitor_text=visit)
        finish_line()
    except (TypeError, AttributeError, NotImplementedError):
        return set()
    return headings


def iter_pdf_context_packets(pdf_path: Path, max_pages: int = 0,
                             packet_target_words: int = EXTRACTION_PACKET_TARGET_WORDS,
                             overlap_words: int = EXTRACTION_PACKET_OVERLAP_WORDS, inspection=None):
    """Yield overlapping, section-aware passages for rich extraction context.

    Packets target roughly 5,600 source tokens while preserving sentence boundaries
    and a small overlap. A later 1-3 sentence evidence span remains the provenance
    for each extracted claim. Short sections naturally produce shorter packets.
    """
    reader = PdfReader(str(pdf_path))
    pages = min(len(reader.pages), max_pages) if max_pages else len(reader.pages)
    sections: list[dict[str, Any]] = []
    current_section: dict[str, Any] | None = None
    content_started = False
    seen_sentences: set[str] = set()
    inspection = inspection if inspection is not None else inspect_pdf(reader, max_pages)
    page_texts: list[tuple[int, str]] = []
    for page_index in range(pages):
        region = inspection['regions'][page_index]
        text = region['body']
        if page_index == 0 or region['chapter_id'] != inspection['regions'][page_index - 1]['chapter_id']:
            current_section = {'heading': region['chapter_title'], 'sentences': [], 'chapter_id': region['chapter_id']}
            sections.append(current_section)
            content_started = True
        if not text.strip():
            continue
        page_texts.append((page_index + 1, text))
        layout_headings = layout_heading_candidates(reader.pages[page_index], text)
        cleaned_lines = [line.strip() for line in text.splitlines() if not JOURNAL_HEADER_RE.match(line)]
        for paragraph in re.split(r"\n\s*\n", "\n".join(cleaned_lines)):
            lines = [SPACE_RE.sub(" ", line).strip() for line in paragraph.splitlines() if line.strip()]
            body_lines: list[str] = []
            line_index = 0
            while line_index < len(lines):
                line = lines[line_index]
                # inspect_pdf has already separated bibliography regions.
                # Ordinary prose beginning "References to ..." is body text.
                heading = _section_name(line) or (line if line in layout_headings else None)
                if not heading and len(lines) == 1:
                    heading = _unnumbered_topic_heading(line)
                if heading:
                    if body_lines and current_section is not None:
                        _append_section_sentences(current_section, "\n".join(body_lines), page_index + 1, seen_sentences)
                    body_lines = []
                    current_section = {"heading": heading, "sentences": [], "chapter_id": region["chapter_id"]}
                    sections.append(current_section)
                    content_started = True
                    line_index += 1
                    continue
                abstract_match = re.match(r"^abstract\s*:?[\s]*(.*)$", line, flags=re.IGNORECASE)
                if abstract_match:
                    if body_lines and current_section is not None:
                        _append_section_sentences(current_section, "\n".join(body_lines), page_index + 1, seen_sentences)
                    body_lines = []
                    current_section = {"heading": "Abstract", "sentences": [], "chapter_id": region["chapter_id"]}
                    sections.append(current_section)
                    content_started = True
                    abstract_body = abstract_match.group(1).strip()
                    body_lines = [abstract_body] if abstract_body else []
                    line_index += 1
                    continue
                if content_started:
                    body_lines.append(line)
                line_index += 1
            if body_lines and current_section is not None:
                _append_section_sentences(current_section, "\n".join(body_lines), page_index + 1, seen_sentences)

    # Some publishers use unnumbered headings or no explicit Abstract marker.
    # The PDF is still usable when text extraction succeeded, so retain sentence
    # provenance under a neutral section instead of reporting a false OCR error.
    if not sections and page_texts:
        fallback_section = {"heading": "Unlabelled paper text", "sentences": []}
        for page, text in page_texts:
            before_references = REFERENCE_HEADING_RE.split(text, maxsplit=1)[0]
            _append_section_sentences(fallback_section, before_references, page, seen_sentences)
        if fallback_section["sentences"]:
            sections.append(fallback_section)

    # Pack one continuous document stream. Many review articles have dozens of
    # short subsections; emitting each subsection independently produced
    # 100-700-word prompts and allowed four claims from every tiny section.
    # Combining adjacent sections restores the intended ~4,000-word context and
    # imposes a meaningful document-level extraction bound while retaining the
    # originating heading on every sentence.
    stream: list[dict[str, Any]] = []
    for section in sections:
        for sentence in section["sentences"]:
            stream.append({**sentence, "section_heading": section["heading"], "chapter_id": section.get("chapter_id", "chapter-1")})

    start = 0
    packet_number = 1
    while start < len(stream):
        end = start
        packet_words = 0
        while end < len(stream) and stream[end]["chapter_id"] == stream[start]["chapter_id"] and (packet_words < packet_target_words or end == start):
            packet_words += word_count(stream[end]["text"])
            end += 1
        packet = stream[start:end]
        if not packet:
            break
        headings = list(dict.fromkeys(str(sentence["section_heading"]) for sentence in packet))
        heading = headings[0] if len(headings) == 1 else f"{headings[0]} -> {headings[-1]}"
        packet_word_count = sum(word_count(sentence["text"]) for sentence in packet)
        yield {
            "section_heading": heading,
            "chapter_id": packet[0]["chapter_id"],
            "section_headings": headings,
            "packet_number": packet_number,
            "page_start": packet[0]["page"],
            "page_end": packet[-1]["page"],
            "word_count": packet_word_count,
            "estimated_token_count": round(packet_word_count * ESTIMATED_TOKENS_PER_WORD),
            "sentences": packet,
        }
        if end >= len(stream):
            break
        if stream[end]["chapter_id"] != stream[start]["chapter_id"]:
            start = end
            packet_number += 1
            continue
        next_start = end
        overlap_count = 0
        while next_start > start + 1 and overlap_count < overlap_words:
            next_start -= 1
            overlap_count += word_count(stream[next_start]["text"])
        start = next_start
        packet_number += 1
