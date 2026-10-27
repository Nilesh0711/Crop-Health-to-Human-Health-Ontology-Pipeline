"""PDF headings, citation syntax and sentence/packet limits; no scientific truth rules."""

from __future__ import annotations

import re
from backend.shared.text_structure import REFERENCE_HEADING_TEXT


DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)


PMCID_RE = re.compile(r"\bPMC\d{4,}\b", re.IGNORECASE)


PMID_RE = re.compile(r"\bPMID\s*[:=]?\s*(\d{5,10})\b", re.IGNORECASE)


SENTENCE_PERIOD_MARKER = "<DOT>"


SCHOLARLY_ABBREVIATION_RE = re.compile(
    r"\b(?:et\s+al|e\.g|i\.e|fig|figs|eq|eqs|ref|refs|vs|dr|prof|no)\.",
    re.IGNORECASE,
)


SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“(\[])")


SECTION_HEADING_RE = re.compile(
    r"^\s*(?P<number>\d{1,2}(?:\.\d+)*)(?P<terminal_dot>\.)?\s+(?P<title>[A-Z][A-Za-z0-9 ,:;()/-]{2,140})\s*$"
)


JOURNAL_HEADER_RE = re.compile(r"^\s*Agronomy\s+\d{4},\s+\d+.*?(?:of\s+\d+)?\s*$", re.IGNORECASE)


REFERENCE_HEADING_RE = re.compile(
    rf"^\s*(?:\d+(?:\.\d+)*\.?\s+)?{REFERENCE_HEADING_TEXT}\b", re.IGNORECASE | re.MULTILINE
)


REFERENCE_ENTRY_RE = re.compile(r"(?m)^\s*(?P<number>\d{1,3})\.\s+(?P<text>.+?)(?=^\s*\d{1,3}\.\s+|\Z)", re.DOTALL)


CITATION_GROUP_RE = re.compile(
    r"\[\s*(?P<numbers>\d{1,3}(?:\s*(?:[,;]|[-\u2013\u2014\ufffd])\s*\d{1,3})*)\s*\]"
)


AUTHOR_YEAR_REFERENCE_RE = re.compile(
    r"(?m)^\s*(?P<author>[^\W\d_][^,\n]{0,80}),\s+[^\n]{0,220}?\b(?P<year>(?:19|20)\d{2}[a-z]?)\.\s*",
    re.UNICODE,
)


APA_AUTHOR_YEAR_REFERENCE_RE = re.compile(
    r"^\s*(?P<author>[^\W\d_][\w'\u2019 -]{1,70}),\s+(?=[A-Z]\s*\.)"
    r".{0,600}?\(\s*(?P<year>(?:19|20)\d{2}[a-z]?)\s*\)\.",
    re.MULTILINE | re.DOTALL | re.UNICODE,
)


AUTHOR_YEAR_CITATION_RE = re.compile(
    r"\b(?P<author>[^\W\d_][\w'\u2019-]{1,70})"
    r"(?:\s+et\s+al\.|\s+(?:and|&)\s+[^\W\d_][\w'\u2019-]{1,70})?"
    r"(?:\s*,\s*|\s*\(\s*)(?P<year>(?:19|20)\d{2}[a-z]?)\s*\)?",
    re.IGNORECASE | re.UNICODE,
)


BARE_AUTHOR_YEAR_CITATION_RE = re.compile(
    r"\b(?P<author>[A-Z][A-Za-z'\u2019-]{1,70})"
    r"(?:\s+et\s+al\.\s*|\s+(?:and|&)\s+[A-Z][A-Za-z'\u2019-]{1,70}\s+)"
    r"(?P<year>(?:19|20)\d{2}[a-z]?)\b",
    re.UNICODE,
)


PARENTHETICAL_SINGLE_AUTHOR_YEAR_RE = re.compile(
    r"(?:^|;)\s*(?P<author>[A-Z][A-Za-z'\u2019-]{1,70})\s+"
    r"(?P<year>(?:19|20)\d{2}[a-z]?)\s*(?=;|$)",
    re.UNICODE,
)


PARENTHETICAL_BLOCK_RE = re.compile(r"\((?P<content>[^()]*(?:19|20)\d{2}[^()]*)\)")


UNDOTTED_SECTION_STEMS = {
    "abstract", "introduction", "background", "methods", "methodology", "materials", "experimental",
    "results", "findings", "discussion", "conclusion", "conclusions", "summary", "review",
    "literature", "acknowledgements", "acknowledgments", "appendix", "supplementary",
}


HEADING_TRAILING_FRAGMENTS = {
    "a", "an", "and", "are", "as", "at", "by", "for", "from", "in", "is", "of", "on", "or", "the", "to", "was", "were", "with",
}


HEADING_INITIAL_LIST_VERBS = {
    "analyse", "analyze", "assess", "compare", "consider", "describe", "determine", "evaluate", "examine", "identify", "investigate", "measure", "provide", "show", "use",
}


EXTRACTION_PACKET_TARGET_WORDS = 4000


EXTRACTION_PACKET_OVERLAP_WORDS = 500


ESTIMATED_TOKENS_PER_WORD = 1.4


PUBLISHER_BOILERPLATE_RE = re.compile(
    r"\b(?:open access|correspondence|copyright|creative commons|author contributions|conflict of interest|"
    r"publisher(?:'s|s)? note|generative ai statement|suggested citation|buy this book|find related titles|"
    r"all rights reserved|table of contents|boxes, figures, and tables|consensus study report|"
    r"(?:received|accepted|published)\s+(?:/|\d)|citation\s+[A-Z][a-z]+|keywords|department of|division of)\b",
    re.IGNORECASE,
)
