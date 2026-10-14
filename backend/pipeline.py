"""Extraction, external validation, and graph construction for one PDF."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from pypdf import PdfReader

from external_validator.config import ONTOLOGY_DIR, PROJECT_ROOT, SCHEMA_VERSION, Settings
from external_validator.llm import OpenCodeClient
from external_validator.nli import NLIValidator
from external_validator.rerank import SemanticReranker
from external_validator.storage import file_hash, stable_hash, utc_now, write_jsonl
from external_validator.workflow import validate_candidate

from .graph import graph_from_candidates, reasoning_paths, write_graphml
from .evaluation import write_evaluation_sample
from .ontology_schema import AUTOMATIC_EXTRACTION_RELATIONS, ENTITY_TYPES, schema_error
from .relevance_validation import classify_document_scope
from .semantic_validation import validate_claim_semantics
from .source_validation import uses_conditional_modality, validate_source_claim
from .vocabulary import normalize_entity


GENERIC_ENTITIES = {"agriculture", "food", "temperature", "yield", "disease", "soil", "pests", "crop", "health", "nutrition", "nutrient",
                    "crop growth", "crop productivity", "soil health", "soil quality", "nutrient levels",
                    "plantdisease", "soilcondition", "climatefactor", "humanexposure", "humanhealthoutcome", "agriculturalpractice"}
SPACE_RE = re.compile(r"\s+")
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
    r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?(?:references|bibliography)\b", re.IGNORECASE | re.MULTILINE
)
REFERENCE_SECTION_HEADING_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?(?:references|bibliography)\s*$",
    re.IGNORECASE | re.MULTILINE,
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
# LongCat is remotely hosted and can receive substantially richer source
# context than a local 8B model. Keep the packet below a conservative
# ~8k-token prompt budget after instructions and priority evidence are added.
EXTRACTION_PACKET_TARGET_WORDS = 4000
EXTRACTION_PACKET_OVERLAP_WORDS = 500
EXTRACTION_MAX_CLAIMS_PER_PACKET = 4
EXTRACTION_OUTPUT_TOKENS = 1800
ESTIMATED_TOKENS_PER_WORD = 1.4
COMPARATIVE_CUE_RE = re.compile(r"\b(?:compared with|compared to|versus|vs\.?|than|relative to)\b", re.IGNORECASE)
PURPOSE_ONLY_RE = re.compile(
    r"^\s*(?:use|use of|application|adoption|deployment)\b.+?\b(?:to|for)\s+"
    r"(?:increase|reduce|control|improve|maintain|avoid|prevent)\b", re.IGNORECASE)
FIGURE_OR_TABLE_RE = re.compile(r"^\s*(?:figure|table)\s*\d", re.IGNORECASE)
NON_EMPIRICAL_RE = re.compile(r"\b(?:no information on|no evidence (?:was )?(?:available|exists)|future research)\b", re.IGNORECASE)
PUBLISHER_BOILERPLATE_RE = re.compile(
    r"\b(?:open access|correspondence|copyright|creative commons|author contributions|conflict of interest|"
    r"publisher(?:'s|s)? note|generative ai statement|suggested citation|buy this book|find related titles|"
    r"all rights reserved|table of contents|boxes, figures, and tables|consensus study report|"
    r"(?:received|accepted|published)\s+(?:/|\d)|citation\s+[A-Z][a-z]+|keywords|department of|division of)\b",
    re.IGNORECASE,
)
ASSOCIATIVE_TRIGGER_RE = re.compile(
    r"\b(?:associated with|association between|correlated with|linked to|relationship between)\b",
    re.IGNORECASE,
)
EXTRACTION_RELATION_CUE_RE = re.compile(
    r"\b(?:affect\w*|influenc\w*|impact\w*|reduc\w*|decreas\w*|lower\w*|suppress\w*|"
    r"inhibit\w*|increas\w*|enhanc\w*|promot\w*|improv\w*|accumulat\w*|contain\w*|"
    r"contaminat\w*|associat\w*|correlat\w*|link(?:ed|s)?|higher|greater)\b",
    re.IGNORECASE,
)
EXTRACTION_PATHWAY_TERM_RE = re.compile(
    r"\b(?:soil|crop|plant|root|microb\w*|fung\w*|nutrient\w*|mineral\w*|contamin\w*|"
    r"pesticid\w*|mycotoxin\w*|arsenic|cadmium|pathogen\w*|disease|exposure|intake|human|"
    r"health|phytochemical\w*|antioxidant\w*)\b",
    re.IGNORECASE,
)
EDITORIAL_NARRATIVE_RE = re.compile(
    r"\b(?:research topic|this review|the narrative|the story|underscores|highlights the need|"
    r"was developed to explore|we hope|future research)\b",
    re.IGNORECASE,
)


def _looks_like_index_listing(text: str) -> bool:
    """Detect extracted contents/figure lists that resemble long sentences."""
    return (len(re.findall(r"\b\d{1,2}-\d{1,3}\b", text)) >= 3
            or len(re.findall(r",\s*\d{1,3}\b", text)) >= 6)


def extraction_priority_sentence_numbers(sentences: list[dict[str, Any]], limit: int = 14) -> list[int]:
    """Point the extractor toward specific mechanistic sentences in a large packet.

    This is an attention hint, not an acceptance rule. Every emitted claim
    still needs exact anchors and the normal semantic/source validation.
    """
    ranked: list[tuple[int, int]] = []
    for number, sentence in enumerate(sentences, 1):
        text = str(sentence.get("text") or "")
        relation_cues = len(EXTRACTION_RELATION_CUE_RE.findall(text))
        pathway_terms = len(EXTRACTION_PATHWAY_TERM_RE.findall(text))
        score = relation_cues * 3 + min(pathway_terms, 6)
        if EDITORIAL_NARRATIVE_RE.search(text):
            score -= 4
        if relation_cues and pathway_terms >= 2:
            ranked.append((score, number))
    return [number for _score, number in sorted(ranked, key=lambda item: (-item[0], item[1]))[:limit]]
GENERIC_ENDPOINTS = {
    "abundance", "benefits", "crop composition", "crop quality", "food security", "health effects",
    "health impacts", "negative health effects", "negative health impacts", "nutritional quality",
    "safety parameters", "yield gap", "yield", "pest and disease", "need to produce crop calories protein",
    "production", "crop performance", "crop performances", "stockpiles", "economic accessibility", "stability of supply", "food availability",
    "food access", "uses of the grain", "negative environmental impacts of agricultural production",
}

EXTRACTION_RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "maxItems": EXTRACTION_MAX_CLAIMS_PER_PACKET,
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "subject_source_mention": {"type": "string"},
                    "relation": {"type": "string", "enum": sorted(AUTOMATIC_EXTRACTION_RELATIONS)},
                    "object": {"type": "string"},
                    "object_source_mention": {"type": "string"},
                    "relation_trigger": {"type": "string"},
                    "subject_type": {"type": "string", "enum": sorted(ENTITY_TYPES)},
                    "object_type": {"type": "string", "enum": sorted(ENTITY_TYPES)},
                    "evidence_sentence_numbers": {
                        "type": "array", "minItems": 1, "maxItems": 3,
                        "items": {"type": "integer", "minimum": 1},
                    },
                    "certainty": {
                        "type": "string",
                        "enum": ["causal", "associative", "conditional", "descriptive"],
                    },
                    "qualifiers": {
                        "type": "object",
                        "properties": {
                            "crop_or_species": {"type": "string"},
                            "population": {"type": "string"},
                            "condition": {"type": "string"},
                            "comparator": {"type": "string"},
                            "metric": {"type": "string"},
                        },
                        "required": ["crop_or_species", "population", "condition", "comparator", "metric"],
                        "additionalProperties": False,
                    },
                },
                "required": ["subject", "subject_source_mention", "relation", "object", "object_source_mention",
                             "relation_trigger", "subject_type", "object_type",
                             "evidence_sentence_numbers", "certainty", "qualifiers"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["claims"],
    "additionalProperties": False,
}

def normalized(value: str) -> str:
    return SPACE_RE.sub(" ", value).strip().casefold()


def word_count(text: str) -> int:
    return len(re.findall(r"\w+", text))


def extraction_shape_error(candidate: dict[str, Any], evidence_span: str) -> str | None:
    """Reject clear extraction artefacts before an LLM validation call.

    These checks address evidence *shape*, not domain truth.  They prevent a
    section-outline recommendation, a figure/table caption, a missing endpoint,
    or an unqualified comparative result from becoming an expensive candidate.
    """
    subject = normalized(str(candidate.get("subject") or ""))
    obj = normalized(str(candidate.get("object") or ""))
    evidence = str(evidence_span or "").strip()
    qualifiers = candidate.get("qualifiers") or {}
    if candidate.get("relation") and candidate.get("relation") not in AUTOMATIC_EXTRACTION_RELATIONS:
        return "relation_not_allowed_in_automatic_extraction"
    if obj in GENERIC_ENDPOINTS or subject in GENERIC_ENDPOINTS:
        return "generic_or_out_of_scope_endpoint"
    if PURPOSE_ONLY_RE.search(evidence):
        return "purpose_or_recommendation_not_empirical_evidence"
    if FIGURE_OR_TABLE_RE.search(evidence):
        return "figure_or_table_caption_not_empirical_evidence"
    if NON_EMPIRICAL_RE.search(evidence):
        return "absence_of_evidence_is_not_a_positive_claim"
    if COMPARATIVE_CUE_RE.search(evidence):
        if not str(qualifiers.get("comparator") or "").strip() or not str(qualifiers.get("metric") or "").strip():
            return "comparative_evidence_missing_comparator_or_metric"
    return None


def extraction_anchor_error(subject_mention: str, object_mention: str, relation_trigger: str) -> str | None:
    """Reject predicate-sized strings masquerading as entity anchors."""
    subject = normalized(subject_mention)
    obj = normalized(object_mention)
    trigger = normalized(relation_trigger)
    if not subject or not obj or not trigger:
        return "missing_source_anchor"
    if subject == obj:
        return "identical_subject_and_object_anchors"
    trigger_pattern = rf"(?<!\w){re.escape(trigger)}(?!\w)"
    if re.search(trigger_pattern, subject) or re.search(trigger_pattern, obj):
        return "entity_anchor_contains_relation_trigger"
    return None


_GENERIC_DISEASE_ENTITY_RE = re.compile(
    r"^(?:increased|decreased|high|low|greater|reduced)?\s*"
    r"(?:disease|infection)\s+(?:intensity|incidence|severity|level|levels|rate|rates)$",
    re.IGNORECASE,
)


def contextual_disease_label(section_heading: str) -> str | None:
    """Return a named disease from a conservative local topic heading."""
    heading = SPACE_RE.sub(" ", str(section_heading or "")).strip()
    if not heading or not re.search(
            r"\b(?:blights?|rusts?|smut|brown spot|mildew|rot|wilt|scab)\b", heading, re.IGNORECASE):
        return None
    label = re.split(r"[:,]", heading, maxsplit=1)[0].strip()
    label = re.sub(
        r"\s+of\s+(?:rice|wheat|maize|corn|barley|oats?|rye|potato|coffee)(?:\s+.*)?$",
        "", label, flags=re.IGNORECASE,
    ).strip()
    return label if 1 < len(label.split()) <= 8 else None


def normalize_contextual_disease_entity(candidate: dict[str, Any], section_heading: str) -> dict[str, str] | None:
    """Replace only a generic disease-intensity label with its exact section topic.

    The source surface mention remains unchanged and source validation must
    still prove the bounded heading-to-mention binding. This normalization is
    useful for retrieval: external papers discuss ``false smut``, not the
    context-free string ``increased disease intensity``.
    """
    subject = str(candidate.get("subject") or "").strip()
    if not _GENERIC_DISEASE_ENTITY_RE.fullmatch(subject):
        return None
    disease = contextual_disease_label(section_heading)
    if not disease:
        return None
    candidate["subject"] = disease
    candidate["subject_type"] = "PlantDisease"
    return {"field": "subject", "original": subject, "normalized": disease,
            "basis": "bounded_local_section_heading"}


_CONTAMINANT_CONTAINER_RE = re.compile(
    r"\b(?:produced|detected|found|present|occurring)\s+in\s+"
    r"(?P<container>(?:infected|contaminated)\s+(?:grains?|kernels?|seeds?))\b",
    re.IGNORECASE,
)


def normalize_contaminant_container_claim(candidate: dict[str, Any], evidence_span: str,
                                           section_heading: str) -> dict[str, str] | None:
    """Repair a disease-as-container error only when the source names a container.

    ``FHB ... mycotoxins may be produced in infected grains`` does not mean a
    disease contains a toxin. The explicit crop material is the composition
    subject, while the disease remains a provenance-bearing condition.
    """
    if not (candidate.get("relation") == "has_contaminant"
            and candidate.get("subject_type") == "PlantDisease"
            and candidate.get("object_type") == "Contaminant"):
        return None
    match = _CONTAMINANT_CONTAINER_RE.search(str(evidence_span or ""))
    if not match:
        return None
    original_subject = str(candidate.get("subject") or "").strip()
    container = match.group("container")
    candidate["subject"] = container
    candidate["subject_type"] = "Crop"
    qualifiers = candidate.setdefault("qualifiers", {})
    existing_crop = str(qualifiers.get("crop_or_species") or "").strip()
    # ``grains`` is no additional restriction when the canonical composition
    # subject is already ``infected grains``.
    if existing_crop and normalized(existing_crop) in normalized(container):
        qualifiers["crop_or_species"] = ""
    if not str(qualifiers.get("condition") or "").strip():
        qualifiers["condition"] = contextual_disease_label(section_heading) or original_subject
    return {
        "field": "subject",
        "original": original_subject,
        "normalized": container,
        "source_mention": container,
        "basis": "explicit_contaminant_container",
    }


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


def pdf_bibliography(pdf_path: Path) -> dict[int | str, dict[str, Any]]:
    """Parse numbered and author-year bibliography entries for citation-first retrieval.

    The full reference text is provenance only.  It is not treated as support
    until an independently retrieved article passage passes entailment.
    """
    try:
        reader = PdfReader(str(pdf_path))
        pages: list[str] = []
        for page in reader.pages:
            extracted = unicodedata.normalize("NFKC", page.extract_text() or "")
            pages.append("\n".join(line for line in extracted.splitlines() if not JOURNAL_HEADER_RE.match(line)))
        text = "\n".join(pages)
    except Exception:
        return {}
    # Use an exact heading and prefer the last occurrence. A broad search for
    # ``References ...`` can start inside article prose and make ordinary body
    # lines look like bibliography entries.
    heading_matches = list(REFERENCE_SECTION_HEADING_RE.finditer(text))
    if not heading_matches:
        return {}
    match = heading_matches[-1]
    reference_text = text[match.end():]
    entries: dict[int | str, dict[str, Any]] = {}
    for entry in REFERENCE_ENTRY_RE.finditer(reference_text):
        number = int(entry.group("number"))
        citation = SPACE_RE.sub(" ", entry.group("text")).strip()
        if not citation or number in entries:
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
        entries.setdefault(key, record)
        base_year_keys.setdefault(f"{author}|{year[:4]}", []).append(key)
    # A citation sometimes omits the a/b/c suffix. Add the suffix-free alias
    # only when it resolves to exactly one bibliography entry.
    for base_key, full_keys in base_year_keys.items():
        if len(set(full_keys)) == 1:
            entries.setdefault(base_key, entries[full_keys[0]])
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


def _qualifier_is_redundant(value: str, *entities: str) -> bool:
    qualifier_terms = re.findall(r"[a-z0-9]+", normalized(value))
    if not qualifier_terms:
        return False
    for entity in entities:
        entity_terms = re.findall(r"[a-z0-9]+", normalized(entity))
        if (qualifier_terms == entity_terms
                or (len(qualifier_terms) > 1 and all(term in entity_terms for term in qualifier_terms))):
            return True
    return False


def normalize_extraction_qualifiers(candidate: dict[str, Any],
                                    subject_source_mention: str = "",
                                    evidence_span: str = "") -> list[dict[str, str]]:
    """Remove duplicated restrictions that would poison external matching.

    The extractor occasionally copied an AgriculturalPractice (for example,
    ``crop rotation and intercropping systems``) into ``crop_or_species``.
    Such a value is not added knowledge and forces every external quote to
    repeat a malformed qualifier. Only exact/redundant values are cleared;
    genuine crop, population, comparator, metric, and condition constraints
    remain untouched. A geographic/farming occurrence context for an atomic
    ``has_contaminant`` edge is source provenance, not a universal claim
    qualifier: it is retained under ``source_context`` so literature from a
    different region may corroborate the generic crop-contaminant relation.
    """
    qualifiers = candidate.get("qualifiers") or {}
    changes: list[dict[str, str]] = []
    for field in ("crop_or_species", "population"):
        value = str(qualifiers.get(field) or "").strip()
        if value and _qualifier_is_redundant(value, str(candidate.get("subject") or ""),
                                             str(candidate.get("object") or "")):
            qualifiers[field] = ""
            changes.append({"field": field, "removed_value": value, "reason": "duplicated_entity"})
    population = str(qualifiers.get("population") or "").strip()
    is_farming_site_context = bool(re.search(r"\b(?:farming|production|cultivation)\b", population, re.IGNORECASE)
                                   and re.search(r"\b(?:in|from|at)\s+[A-Z]", population))
    if candidate.get("relation") == "has_contaminant" and population and is_farming_site_context:
        candidate["source_context"] = population
        qualifiers["population"] = ""
        changes.append({"field": "population", "moved_value": population,
                        "reason": "geographic_farming_occurrence_context"})
    # Extract a source setting even when the model left its optional qualifier
    # blank. This applies to any crop-based farming/production phrase and
    # preserves the location in provenance without changing the graph edge.
    if candidate.get("relation") == "has_contaminant" and not candidate.get("source_context"):
        crop = str(candidate.get("subject") or "").strip()
        surface = str(subject_source_mention or candidate.get("subject_source_mention") or "").strip()
        match = (re.match(rf"(?i)^{re.escape(crop)}[-\s]+based\s+(?P<setting>.+)$", surface)
                 if crop else None)
        if match and re.search(r"\b(?:farming|production|cultivation)\b", match.group("setting"), re.IGNORECASE):
            setting = match.group("setting")
            if evidence_span:
                location = re.search(rf"{re.escape(surface)}\s+(in\s+[A-Z][a-z]+(?:\s+[A-Z][a-z]+){{0,3}})",
                                     evidence_span)
                if location:
                    setting += " " + location.group(1)
            candidate["source_context"] = setting
            changes.append({"field": "source_context", "added_value": setting,
                            "reason": "compound_crop_occurrence_context"})
    candidate["qualifiers"] = qualifiers
    return changes


def anchored_quote(source_text: str, proposed_quote: str) -> str | None:
    """Return the exact extracted-text span when all quote words occur in order."""
    words = re.findall(r"\w+", unicodedata.normalize("NFKC", proposed_quote))
    if len(words) < 5:
        return None
    pattern = r"(?<!\w)" + r"\W+".join(re.escape(word) for word in words) + r"(?!\w)"
    match = re.search(pattern, source_text, flags=re.IGNORECASE)
    return source_text[match.start():match.end()] if match else None


def source_surface_mention(source_text: str, proposed_mention: str) -> str | None:
    """Return a verbatim source mention when its words occur consecutively.

    Extraction is allowed to use a canonical entity name for graph merging, but
    it must also supply the exact surface mention it relied on.  This prevents
    an attractive paraphrase (or a CamelCase label) from becoming the only
    evidence anchor for a claim.
    """
    mention_words = re.findall(r"\w+", unicodedata.normalize("NFKC", str(proposed_mention or "")), flags=re.UNICODE)
    if not mention_words:
        return None
    source = unicodedata.normalize("NFKC", str(source_text or ""))
    tokens = list(re.finditer(r"\w+", source, flags=re.UNICODE))
    wanted = [word.casefold() for word in mention_words]
    available = [token.group(0).casefold() for token in tokens]
    for index in range(len(available) - len(wanted) + 1):
        if available[index:index + len(wanted)] == wanted:
            return source[tokens[index].start():tokens[index + len(wanted) - 1].end()]
    return None


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


def _unnumbered_topic_heading(line: str, continuation: str = "") -> str | None:
    """Recognize conservative disease/topic headings used by older reviews."""
    compact = SPACE_RE.sub(" ", f"{line} {continuation}" if continuation else line).strip()
    if not 4 <= len(compact) <= 120 or compact.endswith((".", ";", "?", "!")):
        return None
    words = re.findall(r"[A-Za-z]+", compact)
    if not 2 <= len(words) <= 18 or not compact[0].isupper():
        return None
    if not re.search(
            r"\b(?:blights?|rusts?|smut|brown spot|mildew|rot|wilt|scab|pathogen|plant disease|"
            r"soil health|crop health|human health|contaminant|mycotoxin|exposure)\b",
            compact, re.IGNORECASE):
        return None
    colon_heading = (":" in compact and not compact.endswith(":")
                     and not re.search(r"[\[\](){}]", compact)
                     and bool(re.search(r":\s*[A-Za-z].+", compact)))
    if colon_heading or ("," in compact and re.search(
            r"\b(?:today|century|Asia|Africa|Europe|America)\s*$", compact, re.IGNORECASE)):
        return compact
    return None


def _append_section_sentences(section: dict[str, Any], paragraph: str, page: int, seen: set[str]) -> None:
    for sentence_index, sentence in enumerate(page_sentences(paragraph), 1):
        sentence_key = normalized(sentence)
        if (len(re.findall(r"\w+", sentence)) < 6 or sentence_key in seen
                or PUBLISHER_BOILERPLATE_RE.search(sentence) or _looks_like_index_listing(sentence)):
            continue
        seen.add(sentence_key)
        section["sentences"].append({"page": page, "page_sentence_index": sentence_index, "text": sentence})


def iter_pdf_context_packets(pdf_path: Path, max_pages: int = 0,
                             packet_target_words: int = EXTRACTION_PACKET_TARGET_WORDS,
                             overlap_words: int = EXTRACTION_PACKET_OVERLAP_WORDS):
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
    reference_section = False
    page_texts: list[tuple[int, str]] = []
    for page_index in range(pages):
        text = unicodedata.normalize("NFKC", reader.pages[page_index].extract_text() or "")
        if not text.strip():
            continue
        page_texts.append((page_index + 1, text))
        cleaned_lines = [line.strip() for line in text.splitlines() if not JOURNAL_HEADER_RE.match(line)]
        for paragraph in re.split(r"\n\s*\n", "\n".join(cleaned_lines)):
            lines = [SPACE_RE.sub(" ", line).strip() for line in paragraph.splitlines() if line.strip()]
            body_lines: list[str] = []
            line_index = 0
            while line_index < len(lines):
                line = lines[line_index]
                # Many articles use headings such as ``6. References``. Stop
                # before those citations can be mistaken for source evidence.
                if REFERENCE_HEADING_RE.match(line):
                    reference_section = True
                    break
                heading = _section_name(line)
                consumed_continuation = False
                if not heading:
                    heading = _unnumbered_topic_heading(line)
                if not heading and line_index + 1 < len(lines):
                    continuation = lines[line_index + 1]
                    if len(continuation.split()) <= 4:
                        heading = _unnumbered_topic_heading(line, continuation)
                        consumed_continuation = heading is not None
                if heading:
                    if body_lines and current_section is not None:
                        _append_section_sentences(current_section, "\n".join(body_lines), page_index + 1, seen_sentences)
                    body_lines = []
                    current_section = {"heading": heading, "sentences": []}
                    sections.append(current_section)
                    content_started = True
                    line_index += 2 if consumed_continuation else 1
                    continue
                abstract_match = re.match(r"^abstract\s*:?[\s]*(.*)$", line, flags=re.IGNORECASE)
                if abstract_match:
                    if body_lines and current_section is not None:
                        _append_section_sentences(current_section, "\n".join(body_lines), page_index + 1, seen_sentences)
                    body_lines = []
                    current_section = {"heading": "Abstract", "sentences": []}
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
            if reference_section:
                break
        if reference_section:
            break

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
            stream.append({**sentence, "section_heading": section["heading"]})

    start = 0
    packet_number = 1
    while start < len(stream):
        end = start
        packet_words = 0
        while end < len(stream) and (packet_words < packet_target_words or end == start):
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
        next_start = end
        overlap_count = 0
        while next_start > start + 1 and overlap_count < overlap_words:
            next_start -= 1
            overlap_count += word_count(stream[next_start]["text"])
        start = next_start
        packet_number += 1


def evidence_span_from_item(item: dict[str, Any], packet_sentences: list[dict[str, Any]]) -> tuple[list[int], list[dict[str, Any]], str] | None:
    """Resolve the model-selected 1-3 sentence proof span inside one context packet."""
    numbers = item.get("evidence_sentence_numbers")
    if isinstance(numbers, list) and 1 <= len(numbers) <= 3 and not any(isinstance(number, bool) or not isinstance(number, int) for number in numbers):
        indexes = sorted(set(numbers))
        if len(indexes) == len(numbers) and indexes == list(range(indexes[0], indexes[0] + len(indexes))) and 1 <= indexes[0] <= indexes[-1] <= len(packet_sentences):
            evidence_sentences = [packet_sentences[index - 1] for index in indexes]
            return indexes, evidence_sentences, " ".join(sentence["text"] for sentence in evidence_sentences)

    # Preserve exact provenance if a model returns a valid quote but omits the
    # requested sentence number. Only a quote contained in one packet sentence is
    # accepted as a compatibility fallback.
    quote = str(item.get("quote", "")).strip()
    for index, sentence in enumerate(packet_sentences, 1):
        if anchored_quote(sentence["text"], quote):
            return [index], [sentence], sentence["text"]
    return None


def extract_claims(pdf_path: Path, client: OpenCodeClient, max_pages: int,
                   emit: Callable[[str, str], None], packets: list[dict[str, Any]] | None = None,
                   document_scope: dict[str, Any] | None = None,
                   source_identifiers: dict[str, str] | None = None,
                   claims_per_packet: int = EXTRACTION_MAX_CLAIMS_PER_PACKET
                   ) -> tuple[list[dict[str, Any]], dict[str, int], list[dict[str, Any]]]:
    digest = file_hash(pdf_path)
    document_id = "local-" + stable_hash(f"{pdf_path.stem}|{digest}", 32)
    source_title = source_paper_title(pdf_path)
    source_identifiers = source_identifiers or source_article_identifiers(pdf_path)
    system = ("Extract only nontrivial, directly stated scientific relations from the supplied SECTION CONTEXT PACKET. "
              "Return JSON {\"claims\":[{\"subject\":...,\"subject_source_mention\":...,\"relation\":...,"
              "\"object\":...,\"object_source_mention\":...,\"relation_trigger\":...,\"subject_type\":...,"
              "\"object_type\":...,\"evidence_sentence_numbers\":[1],"
              "\"certainty\":\"causal|associative|conditional|descriptive\",\"qualifiers\":{\"crop_or_species\":\"\",\"population\":\"\",\"condition\":\"\",\"comparator\":\"\",\"metric\":\"\"}}]}. "
              "Use only these entity types: AgriculturalPractice (management intervention, not an organism), ClimateFactor, "
              "SoilMaterial (soil, soil fraction, or amendment), SoilCondition (abiotic soil state or process), "
              "SoilChemicalProperty (soil pH, chemical concentration, or availability), SoilBiota (living soil microbes and fungi), "
              "Crop (plant or harvested crop material), FoodProduct (food or processed edible material), "
              "PlantDisease (named infection or disease), PlantPest (pest organism), PlantProcess (plant uptake or biological process), "
              "PlantTrait (crop composition, quality, resistance level or other measurable characteristic), "
              "Nutrient (beneficial chemical or content), Contaminant (harmful chemical or mycotoxin), HumanExposure (intake/contact), "
              "HumanBiologicalProcess (human biomarker, immune, metabolic or epigenetic process), "
              "HumanHealthOutcome (human disease or health effect, never crop yield). "
              "Use only relations affects, reduces, increases, has_nutrient, has_contaminant, contains, and has_trait. Do not use causes, "
              "leads_to, deficiency_causes, higher_than, lower_than, or associated_with in this starter pipeline. Association-only and comparative "
              "observations require study-design, population, and comparator modelling that is not yet part of the automatic main graph; return no claim "
              "for them. Use has_trait only for Crop/FoodProduct -> PlantTrait. "
              "Use has_nutrient/has_contaminant/contains only for soil/crop/food material -> Nutrient or Contaminant. "
              "Do not put ontology type labels such as HumanHealthOutcome in subject or object names. "
              "Use the whole packet to understand study context, abbreviations, mechanisms, and qualifiers. "
              "For every claim, select one to three consecutive numbered evidence sentences that directly state the subject, relation, and object. "
              "subject_source_mention, object_source_mention, and relation_trigger MUST be exact consecutive words copied "
              "from those selected evidence sentences. Entity mentions must be concise noun phrases: never copy the whole sentence, "
              "never include the relation trigger inside an entity mention, and never use the same text for both endpoints. "
              "They are mandatory proof anchors, not paraphrases. "
              "subject and object may be concise graph labels, but must not be CamelCase ontology labels or substitute a "
              "different intervention, population, material, crop, or outcome for the exact source mention. "
              "Resolve pronouns only if their antecedent is unambiguous within the selected evidence sentences. "
              "Never combine separate mechanisms into a multi-step claim. If the source says X affects M and M affects Y "
              "(including wording such as 'X forms M, which increases Y'), extract the directly stated X->M and M->Y edges; "
              "never collapse that wording into X->Y. "
              "Encode the relation that the sentence actually states. For example, 'mycotoxins may be produced in infected grains' "
              "supports <infected grains, has_contaminant, mycotoxins> with conditional certainty; it does not support "
              "<disease, increases, mycotoxins>. Likewise, 'milled rice rate decreases as infected grains increase' should use "
              "the infected grains and milled-rice rate as endpoints, not collapse the section topic into a direct cause. "
              "When a local disease heading disambiguates those endpoints, preserve that disease in qualifiers.condition "
              "instead of substituting it for an endpoint. "
              "The accepted path is Agriculture/Climate -> Soil -> Crop/Disease -> Nutrient/Contaminant -> Human Exposure -> Health Outcome. "
              "A directly evidenced relation may skip a stage; do not invent intermediate entities. "
              "Exclude economics, infrastructure, trade, generic food security, and yield-only claims. "
              "Qualify entities with species, cultivar, material, comparator, exposure route, dose/condition, and outcome context "
              "whenever they constrain the finding. crop_or_species must contain only an actual crop or biological species, never "
              "a farming practice such as crop rotation, intercropping, crop diversity, or planting. Do not repeat the subject or "
              "object in a qualifier. Empty qualifier fields are correct when the evidence states no such restriction. "
              "A result in carrots must not become a universal AMF claim; a cultivar comparison "
              "must retain both cultivar names and the metric. Skip locations, spatial patterns, maps, figures, tables, captions, "
              "section outlines, recommendations (for example 'use X to increase Y'), model-performance statistics, methods, "
              "measurements, generic background claims, and inferred links. "
              "Do not discard a directly reported hedged relation: preserve may/can/thought/suggested wording as conditional certainty. "
              "Use causal certainty only for an unhedged explicit causal statement; increases/reduces may be descriptive findings. "
              "Before returning claims, prefer the most specific mechanisms in the packet: named organisms, management interventions, "
              "soil properties, nutrients, contaminants, exposure routes, and health outcomes. Broad statements such as soil health "
              "improves crop quality must not consume a claim slot when a more specific supported mechanism is available. "
              f"Return at most {claims_per_packet} distinct, high-value eligible claims supported by a local evidence span; do not try to cover every sentence. "
              "There is no target claim count; zero is better than a weak claim. Do not repeat the evidence text in another field; sentence numbers are sufficient. "
              "A sentence saying linked or associated supports only associated_with, never causes, increases, or reduces. "
              "Never use these standalone generic entity names: soil, soil health, crop, crop productivity, nutritional quality, "
              "health, nutrition, yield, disease, pests, or temperature. Use a specific source phrase such as degraded soils, "
              "soil organic matter, crop cadmium concentration, or a named disease when the evidence supports it. "
              "If no claim has a direct one-to-three-sentence proof span, return {\"claims\":[]}.")
    candidates: list[dict[str, Any]] = []
    seen_occurrences: dict[tuple[Any, ...], dict[str, Any]] = {}
    rejected: Counter[str] = Counter()
    rejection_records: list[dict[str, Any]] = []
    bibliography = pdf_bibliography(pdf_path)
    response_schema = json.loads(json.dumps(EXTRACTION_RESPONSE_SCHEMA))
    response_schema["properties"]["claims"]["maxItems"] = claims_per_packet
    packets = packets if packets is not None else list(iter_pdf_context_packets(pdf_path, max_pages))
    if not packets:
        raise ValueError("The PDF has no extractable text. OCR is required before extraction.")
    for index, packet in enumerate(packets, 1):
        packet_sentences = packet["sentences"]
        packet_text = "\n".join(
            f"[{number}] [Section: {sentence.get('section_heading', packet['section_heading'])}] {sentence['text']}"
            for number, sentence in enumerate(packet_sentences, 1)
        )
        priority_numbers = extraction_priority_sentence_numbers(packet_sentences)
        # A generative model can underweight a bare list of sentence numbers in a long
        # prompt and then spend the limited claim slots on the first broad
        # statements it sees. Repeat only the ranked evidence candidates as a
        # compact attention index. They keep their original sentence numbers,
        # so this does not create a second evidence source or weaken the
        # exact-span provenance checks below.
        priority_index = "\n".join(
            f"[{number}] [Section: {packet_sentences[number - 1].get('section_heading', packet['section_heading'])}] "
            f"{packet_sentences[number - 1]['text']}"
            for number in priority_numbers
        )
        emit("info", f"Extracting {packet['section_heading']} (pages {packet['page_start']}-{packet['page_end']}), packet {packet['packet_number']} ({index}/{len(packets)}; {packet['word_count']} words, ~{packet['estimated_token_count']} tokens)")
        try:
            result = client.chat_json(system, f"Source paper: {source_title}\n"
                                      f"Section: {packet['section_heading']}\n"
                                      f"Pages: {packet['page_start']}-{packet['page_end']}\n"
                                      "PRIORITY EVIDENCE INDEX (ranked; prefer the most specific eligible claims here):\n"
                                      f"{priority_index or '(no high-value candidate detected)'}\n"
                                      f"SECTION CONTEXT PACKET:\n{packet_text}", max_tokens=EXTRACTION_OUTPUT_TOKENS,
                                      json_schema=response_schema)
        except Exception as exc:
            emit("warning", f"Extraction response failed for {packet['section_heading']}: {exc}")
            continue
        extracted_items = result.get("claims", [])
        if not isinstance(extracted_items, list):
            emit("warning", f"{packet['section_heading']}: model returned a non-list claims field")
            continue
        for item in extracted_items:
            if not isinstance(item, dict):
                rejected["malformed"] += 1
                rejection_records.append({"reason": "malformed", "packet_number": packet["packet_number"],
                                          "section_heading": packet["section_heading"], "proposed_claim": {}})
                continue
            subject = str(item.get("subject", "")).strip()
            proposed_subject_mention = str(item.get("subject_source_mention", "")).strip()
            relation = str(item.get("relation", "")).strip().lower()
            obj = str(item.get("object", "")).strip()
            proposed_object_mention = str(item.get("object_source_mention", "")).strip()
            proposed_relation_trigger = str(item.get("relation_trigger", "")).strip()
            candidate = {"subject": subject, "relation": relation, "object": obj,
                         "subject_type": str(item.get("subject_type", "")).strip(),
                         "object_type": str(item.get("object_type", "")).strip(),
                         "certainty": str(item.get("certainty", "")).strip().lower(),
                         "relation_trigger": proposed_relation_trigger}
            raw_qualifiers = item.get("qualifiers")
            candidate["qualifiers"] = {
                key: str(raw_qualifiers.get(key, "")).strip()[:160]
                for key in ("crop_or_species", "population", "condition", "comparator", "metric")
            } if isinstance(raw_qualifiers, dict) else {}
            evidence = evidence_span_from_item(item, packet_sentences)
            if not subject or not obj or evidence is None or "," in subject or "," in obj:
                rejected["malformed"] += 1
                rejection_records.append({"reason": "malformed", "packet_number": packet["packet_number"],
                                          "section_heading": packet["section_heading"],
                                          "proposed_claim": candidate})
                continue
            evidence_numbers, evidence_sentences, evidence_span = evidence
            evidence_heading = str(evidence_sentences[0].get("section_heading") or packet["section_heading"])

            def record_rejection(reason: str) -> None:
                rejection_records.append({
                    "reason": reason,
                    "packet_number": packet["packet_number"],
                    "section_heading": packet["section_heading"],
                    "proposed_claim": dict(candidate),
                    "source_evidence_sentence_numbers": evidence_numbers,
                    "source_evidence_span": evidence_span,
                })

            if normalized(subject) in GENERIC_ENTITIES or normalized(obj) in GENERIC_ENTITIES:
                rejected["generic_entity"] += 1
                record_rejection("generic_entity")
                continue
            if relation in {"increases", "reduces"} and ASSOCIATIVE_TRIGGER_RE.search(proposed_relation_trigger):
                rejected["association_upgraded_to_directional_relation"] += 1
                record_rejection("association_upgraded_to_directional_relation")
                continue
            shape_error = extraction_shape_error(candidate, evidence_span)
            if shape_error:
                rejected[shape_error] += 1
                record_rejection(shape_error)
                continue
            subject_source_mention = source_surface_mention(evidence_span, proposed_subject_mention)
            object_source_mention = source_surface_mention(evidence_span, proposed_object_mention)
            relation_trigger = source_surface_mention(evidence_span, proposed_relation_trigger)
            if not subject_source_mention or not object_source_mention or not relation_trigger:
                rejected["unanchored_evidence"] += 1
                record_rejection("unanchored_evidence")
                continue
            anchor_error = extraction_anchor_error(subject_source_mention, object_source_mention, relation_trigger)
            if anchor_error:
                rejected[anchor_error] += 1
                record_rejection(anchor_error)
                continue
            extraction_normalizations: list[dict[str, str]] = []
            container_normalization = normalize_contaminant_container_claim(
                candidate, evidence_span, evidence_heading
            )
            if container_normalization:
                extraction_normalizations.append(container_normalization)
                subject_source_mention = container_normalization["source_mention"]
                subject = str(candidate["subject"])
            qualifier_adjustments = normalize_extraction_qualifiers(
                candidate, subject_source_mention, evidence_span
            )
            # Exact modal wording is safer than asking the same generative
            # model to self-report its certainty. This normalization only
            # weakens a claim (for example, ``can reduce`` -> conditional) and
            # retains the model's original label for auditability.
            if uses_conditional_modality(evidence_span, relation_trigger) and candidate["certainty"] != "conditional":
                candidate["extraction_certainty"] = candidate["certainty"]
                candidate["certainty"] = "conditional"
            evidence_page = evidence_sentences[0]["page"]
            cited_numbers = citation_numbers(evidence_span)
            cited_keys = author_year_citation_keys(evidence_span)
            reference_keys: list[int | str] = [*cited_numbers, *cited_keys]
            cited_references: list[dict[str, Any]] = []
            seen_reference_ids: set[str] = set()
            for reference_key in reference_keys:
                reference = bibliography.get(reference_key)
                if not reference:
                    continue
                identity = str(reference.get("citation_key") or reference.get("reference_number"))
                if identity in seen_reference_ids:
                    continue
                seen_reference_ids.add(identity)
                cited_references.append(reference)
            contextual_entity_normalization = normalize_contextual_disease_entity(candidate, evidence_heading)
            if contextual_entity_normalization:
                extraction_normalizations.append(contextual_entity_normalization)
            subject = str(candidate["subject"])
            evidence_record = {
                "page": evidence_page,
                "page_start": packet["page_start"],
                "page_end": packet["page_end"],
                "section_heading": evidence_heading,
                "sentence_numbers": evidence_numbers,
                "evidence_span": evidence_span,
                "subject_source_mention": subject_source_mention,
                "object_source_mention": object_source_mention,
                "relation_trigger": relation_trigger,
                "certainty": candidate["certainty"],
                "subject_type": candidate["subject_type"],
                "object_type": candidate["object_type"],
                "qualifiers": dict(candidate["qualifiers"]),
                "source_context": candidate.get("source_context", ""),
                "citation_numbers": cited_numbers,
                "citation_keys": cited_keys,
                "references": cited_references,
            }
            # Only an exact same-evidence assertion is deduplicated. Competing
            # model qualifier guesses must not create repeated validation work
            # for the same subject/relation/object/span; source spans, types,
            # and modalities still remain separate assertions.
            key = (normalized(subject), relation, normalized(obj), candidate["certainty"],
                   candidate["subject_type"], candidate["object_type"], evidence_page,
                   normalized(evidence_span))
            if key in seen_occurrences:
                existing = seen_occurrences[key]
                existing.setdefault("source_evidence_records", []).append(evidence_record)
                existing["source_citation_numbers"] = sorted(set(existing.get("source_citation_numbers", [])) | set(cited_numbers))
                existing["source_citation_keys"] = sorted(set(existing.get("source_citation_keys", [])) | set(cited_keys))
                known = {str(item.get("citation_key") or item.get("reference_number"))
                         for item in existing.get("source_references", [])}
                existing.setdefault("source_references", []).extend(
                    item for item in cited_references
                    if str(item.get("citation_key") or item.get("reference_number")) not in known
                )
                rejected["overlap_occurrence_deduplicated"] += 1
                record_rejection("overlap_occurrence_deduplicated")
                continue
            candidate_id = "candidate-" + stable_hash(
                f"{document_id}|{normalized(subject)}|{relation}|{normalized(obj)}|"
                f"{candidate['certainty']}|{candidate['subject_type']}|{candidate['object_type']}|"
                f"{json.dumps(candidate['qualifiers'], sort_keys=True)}|{evidence_page}|{normalized(evidence_span)}", 32)
            candidate.update({"schema_version": SCHEMA_VERSION, "candidate_id": candidate_id,
                              "source_document_id": document_id, "source_paper_title": source_title,
                               "source_doi": source_identifiers.get("source_doi", ""),
                               "source_pmcid": source_identifiers.get("source_pmcid", ""),
                               "source_pmid": source_identifiers.get("source_pmid", ""),
                              "source_pdf_sha256": digest, "source_page": evidence_page,
                              "source_page_start": packet["page_start"], "source_page_end": packet["page_end"],
                              "source_section_heading": evidence_heading,
                              "source_evidence_section_heading": evidence_heading,
                              "source_packet_number": packet["packet_number"],
                              "source_context_estimated_tokens": packet["estimated_token_count"],
                              "source_evidence_sentence_numbers": evidence_numbers,
                              "source_evidence_span": evidence_span,
                              "subject_source_mention": subject_source_mention,
                              "object_source_mention": object_source_mention,
                              "relation_trigger": relation_trigger,
                              "source_citation_numbers": cited_numbers,
                              "source_citation_keys": cited_keys,
                              "source_references": cited_references,
                              "source_evidence_records": [evidence_record],
                              "qualifier_adjustments": qualifier_adjustments,
                              "source_context": candidate.get("source_context", ""),
                              "contextual_entity_normalization": contextual_entity_normalization,
                              "extraction_normalizations": extraction_normalizations,
                              "document_scope": (document_scope or {}).get("scope", "uncertain"),
                              "source_context_packet": " ".join(sentence["text"] for sentence in packet_sentences),
                              "extraction_type_variants": [{"subject_type": candidate["subject_type"],
                                                            "object_type": candidate["object_type"]}],
                              })
            candidate["schema_error"] = schema_error(candidate)
            candidates.append(candidate)
            seen_occurrences[key] = candidate
        emit("success", f"{packet['section_heading']}: {len(candidates)} accepted candidates so far")
    emit("info", f"Extraction complete: {len(candidates)} unique candidates; "
         f"{rejected.get('overlap_occurrence_deduplicated', 0)} overlap duplicates removed; "
         f"{sum(value for key, value in rejected.items() if key != 'overlap_occurrence_deduplicated')} discarded ({dict(rejected)})")
    return candidates, dict(rejected), rejection_records


def run_pipeline(pdf_path: Path, max_pages: int, max_claims: int, articles_per_claim: int,
                 emit: Callable[[str, str], None], claims_per_packet: int | None = None) -> dict[str, Any]:
    output_dir = ONTOLOGY_DIR / pdf_path.stem
    output_dir.mkdir(parents=True, exist_ok=True)
    settings = Settings()
    claims_per_packet = claims_per_packet or settings.extraction_claims_per_packet
    if not 1 <= claims_per_packet <= 8:
        raise ValueError("claims_per_packet must be between 1 and 8")
    available_models = OpenCodeClient(settings).health_check().get("models", [])
    if settings.extraction_model not in available_models:
        raise RuntimeError(f"OpenCode extraction model {settings.extraction_model} is unavailable")
    if settings.validation_model not in available_models:
        raise RuntimeError(f"OpenCode validation model {settings.validation_model} is unavailable")
    extractor = OpenCodeClient(settings, settings.extraction_model)
    client = OpenCodeClient(settings, settings.validation_model)
    emit("success", f"Connected to OpenCode extraction model {settings.extraction_model}")
    emit("success", f"Connected to OpenCode validation model {settings.validation_model}")
    packets = list(iter_pdf_context_packets(pdf_path, max_pages))
    if not packets:
        raise ValueError("The PDF has no extractable text. OCR is required before extraction.")
    source_identifiers = source_article_identifiers(pdf_path)
    emit("stage", "Checking PDF text quality and pathway relevance")
    try:
        document_scope = classify_document_scope(source_paper_title(pdf_path), packets, client)
    except Exception as exc:
        document_scope = {
            "scope": "uncertain",
            "text_quality": "usable",
            "rationale": f"Document scope check failed; extraction was retained conservatively: {exc}",
            "quality_signals": {},
        }
        emit("warning", document_scope["rationale"])
    (output_dir / "document_scope.json").write_text(json.dumps(document_scope, ensure_ascii=False, indent=2), encoding="utf-8")
    emit("info", f"Document scope: {document_scope['scope']}; text quality: {document_scope['text_quality']}")
    if document_scope["text_quality"] == "unusable":
        # Persist a terminal summary before raising so the frontend does not
        # later mislabel this intentional quality stop as an unstarted PDF.
        blocked_summary = {
            "pdf": pdf_path.name,
            "pdf_sha256": file_hash(pdf_path),
            "model": settings.extraction_model,
            "extraction_model": settings.extraction_model,
            "validation_model": settings.validation_model,
            "completed_at": utc_now(),
            "run_status": "blocked_text_quality",
            "schema_version": SCHEMA_VERSION,
            "claims_per_packet": claims_per_packet,
            "llm_calls": {
                "extraction": extractor.request_metrics,
                "validation": client.request_metrics,
                "total_requests": extractor.request_metrics["requests"] + client.request_metrics["requests"],
            },
            "document_scope": document_scope,
            "extracted": 0,
            "schema_valid": 0,
            "semantic_accepted": 0,
            "source_entailed": 0,
            "externally_evaluated": 0,
            "graph": {"nodes": 0, "edges": 0, "corroborated": 0, "supported": 0},
            "source_graph": {"nodes": 0, "edges": 0, "corroborated": 0, "supported": 0, "source_supported": 0},
            "audit_graph": {"nodes": 0, "edges": 0, "corroborated": 0, "supported": 0},
            "reasoning_paths": 0,
        }
        (output_dir / "summary.json").write_text(json.dumps(blocked_summary, ensure_ascii=False, indent=2), encoding="utf-8")
        raise ValueError("The PDF text is too degraded for faithful extraction. OCR or a cleaner PDF is required.")
    if document_scope["scope"] == "out_of_scope":
        emit("warning", "Document scope screening found no clear pathway evidence; extraction will still inspect each packet conservatively")
    elif document_scope["scope"] == "upstream_only":
        emit("info", "Paper is upstream-only; retained for evidence-backed pathway bridges, not treated as a health-outcome paper")
    emit("stage", "Extracting evidence-anchored claims from PDF")
    candidates, extraction_rejection_counts, extraction_rejections = extract_claims(
        pdf_path, extractor, max_pages, emit, packets=packets, document_scope=document_scope,
        source_identifiers=source_identifiers, claims_per_packet=claims_per_packet,
    )
    write_jsonl(output_dir / "candidates.jsonl", candidates)
    write_jsonl(output_dir / "extraction_rejections.jsonl", extraction_rejections)
    tuples = "\n".join(
        f"<{item['subject']}, {item['relation']}, {item['object']}, {item['subject_type']}, {item['object_type']}>\n"
        f"# evidence: \"{item['source_evidence_span']}\""
        for item in candidates
    )
    (output_dir / "raw_triplets.txt").write_text(tuples + ("\n" if tuples else ""), encoding="utf-8")
    structurally_valid = [item for item in candidates if not item["schema_error"]]
    write_jsonl(output_dir / "schema_valid_candidates.jsonl",
                [{"candidate_id": item["candidate_id"]} for item in structurally_valid])
    emit("info", f"Controlled vocabulary accepted {len(structurally_valid)}/{len(candidates)} candidates")

    emit("stage", f"Checking semantic fit for {len(structurally_valid)} claims")
    semantic_results: list[dict[str, Any]] = []
    write_jsonl(output_dir / "semantic_validation.jsonl", semantic_results)
    for position, candidate in enumerate(structurally_valid, 1):
        emit("info", f"Semantic claim {position}/{len(structurally_valid)}: {candidate['subject']} -> {candidate['relation']} -> {candidate['object']}")
        try:
            semantic_result = validate_claim_semantics(candidate, client)
        except Exception as exc:
            semantic_result = {"candidate_id": candidate["candidate_id"], "decision": "needs_review",
                               "rationale": f"Semantic check failed: {exc}"}
            emit("warning", semantic_result["rationale"])
        candidate["semantic_validation"] = semantic_result
        if semantic_result.get("decision") == "accepted" and semantic_result.get("action") == "normalize_types":
            normalized_types = semantic_result.get("normalized_types") or {}
            subject_type = normalized_types.get("subject_type")
            object_type = normalized_types.get("object_type")
            if subject_type in ENTITY_TYPES and object_type in ENTITY_TYPES:
                candidate["extraction_types"] = {
                    "subject_type": candidate["subject_type"],
                    "object_type": candidate["object_type"],
                }
                candidate["subject_type"] = subject_type
                candidate["object_type"] = object_type
                semantic_result["normalization_applied"] = True
                emit("info", f"Normalized entity types: {candidate['subject_type']} → {candidate['object_type']}")
            else:
                semantic_result.update({
                    "decision": "needs_review",
                    "action": "review",
                    "rationale": f"{semantic_result.get('rationale', '')} Invalid normalized entity types.",
                    "normalization_applied": False,
                })
        candidate["subject_normalization"] = normalize_entity(candidate["subject"], candidate["subject_type"])
        candidate["object_normalization"] = normalize_entity(candidate["object"], candidate["object_type"])
        semantic_results.append(semantic_result)
        write_jsonl(output_dir / "semantic_validation.jsonl", semantic_results)
        write_jsonl(output_dir / "candidates.jsonl", candidates)
        emit("success", f"Semantic status: {semantic_result['decision']}")

    semantic_accepted = [candidate for candidate in structurally_valid
                         if candidate["semantic_validation"]["decision"] == "accepted"]
    write_jsonl(output_dir / "semantic_valid_candidates.jsonl",
                [{"candidate_id": item["candidate_id"]} for item in semantic_accepted])
    emit("stage", f"Checking source evidence for {len(semantic_accepted)} claims")
    source_results: list[dict[str, Any]] = []
    write_jsonl(output_dir / "source_validation.jsonl", source_results)
    for position, candidate in enumerate(semantic_accepted, 1):
        emit("info", f"Source claim {position}/{len(semantic_accepted)}: {candidate['subject']} → {candidate['relation']} → {candidate['object']}")
        try:
            source_result = validate_source_claim(candidate, client)
        except Exception as exc:
            source_result = {"candidate_id": candidate["candidate_id"], "decision": "needs_review",
                             "rationale": f"Source check failed: {exc}"}
            emit("warning", source_result["rationale"])
        candidate["source_validation"] = source_result
        source_results.append(source_result)
        write_jsonl(output_dir / "source_validation.jsonl", source_results)
        write_jsonl(output_dir / "candidates.jsonl", candidates)
        emit("success", f"Source status: {source_result['decision']}")
    source_entailed = [candidate for candidate in semantic_accepted
                       if candidate["source_validation"]["decision"] == "entailed"]
    write_jsonl(output_dir / "source_entailed_candidates.jsonl",
                [{"candidate_id": item["candidate_id"]} for item in source_entailed])
    results: list[dict[str, Any]] = []
    write_jsonl(output_dir / "validation_runs.jsonl", results)
    initial_graph = graph_from_candidates(candidates, results, view="main")
    initial_source_graph = graph_from_candidates(candidates, results, view="source")
    initial_audit_graph = graph_from_candidates(candidates, results, view="audit")
    (output_dir / "graph.json").write_text(json.dumps(initial_graph, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "source_graph.json").write_text(json.dumps(initial_source_graph, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "audit_graph.json").write_text(json.dumps(initial_audit_graph, ensure_ascii=False, indent=2), encoding="utf-8")
    selected = source_entailed[:max_claims] if max_claims else source_entailed
    emit("stage", f"Validating {len(selected)} claims against Europe PMC and OpenAlex literature")
    nli_validator: NLIValidator | None = None
    if selected and settings.enable_nli:
        emit("info", f"Loading external contradiction checker {settings.nli_model} on {settings.nli_device}")
        try:
            nli_validator = NLIValidator(settings)
            nli_validator.warmup()
            emit("success", "External contradiction checker ready")
        except Exception as exc:
            emit("warning", f"External contradiction checker unavailable: {exc}")
    reranker = SemanticReranker(settings) if selected else None
    if reranker:
        emit("info", f"Loading semantic reranker {settings.embedding_model}")
        try:
            backend = reranker.warmup()
            emit("success", f"Semantic reranker ready ({backend})")
        except Exception as exc:
            emit("error", f"Semantic reranker failed before retrieval: {exc}")
            results.extend({"schema_version": SCHEMA_VERSION, "candidate_id": candidate["candidate_id"],
                            "validation_status": "pipeline_error", "error": str(exc)}
                           for candidate in selected)
            write_jsonl(output_dir / "validation_runs.jsonl", results)
            selected = []
    for position, candidate in enumerate(selected, 1):
        emit("info", f"Claim {position}/{len(selected)}: {candidate['subject']} → {candidate['relation']} → {candidate['object']}")
        try:
            result = validate_candidate(candidate, client, reranker, settings, articles_per_claim, 3,
                                        progress=lambda message: emit("info", message),
                                        nli_validator=nli_validator)
            # candidates.jsonl is the canonical candidate store. Avoid embedding
            # its large source context again in every external-validation row.
            result.pop("candidate", None)
        except Exception as exc:
            result = {"schema_version": SCHEMA_VERSION, "candidate_id": candidate["candidate_id"],
                      "validation_status": "pipeline_error", "error": str(exc)}
            emit("error", f"Claim failed: {exc}")
        results.append(result)
        write_jsonl(output_dir / "validation_runs.jsonl", results)
        emit("success", f"Claim status: {result['validation_status']}")
    emit("stage", "Building knowledge graph")
    graph = graph_from_candidates(candidates, results, view="main")
    source_graph = graph_from_candidates(candidates, results, view="source")
    audit_graph = graph_from_candidates(candidates, results, view="audit")
    (output_dir / "graph.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "source_graph.json").write_text(json.dumps(source_graph, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "audit_graph.json").write_text(json.dumps(audit_graph, ensure_ascii=False, indent=2), encoding="utf-8")
    write_graphml(graph, output_dir / "knowledge_graph.graphml")
    paths = reasoning_paths(graph)
    evaluation_path = write_evaluation_sample(output_dir, candidates, results)
    summary = {"pdf": pdf_path.name, "pdf_sha256": file_hash(pdf_path), "model": settings.extraction_model,
               "extraction_model": settings.extraction_model, "validation_model": settings.validation_model, "completed_at": utc_now(),
               "run_status": "completed",
               "schema_version": SCHEMA_VERSION,
               "claims_per_packet": claims_per_packet,
               "external_judge_workers": settings.external_judge_workers,
               "llm_calls": {
                   "extraction": extractor.request_metrics,
                   "validation": client.request_metrics,
                   "total_requests": extractor.request_metrics["requests"] + client.request_metrics["requests"],
               },
               "embedding_model": settings.embedding_model,
               "cross_encoder_model": settings.cross_encoder_model,
               "nli_model": settings.nli_model if settings.enable_nli else None,
               "extracted": len(candidates), "schema_valid": len(structurally_valid),
               "semantic_accepted": len(semantic_accepted),
               "semantic_type_normalized": sum(item.get("action") == "normalize_types"
                                                and item.get("normalization_applied") is True
                                                for item in semantic_results),
               "semantic_needs_review": sum(item["decision"] == "needs_review" for item in semantic_results),
               "semantic_rejected": sum(item["decision"] == "rejected" for item in semantic_results),
               "source_entailed": len(source_entailed),
               "source_needs_review": sum(item["decision"] == "needs_review" for item in source_results),
               "source_unsupported": sum(item["decision"] == "unsupported" for item in source_results),
               "externally_evaluated": len(results),
               "extraction_rejections": sum(value for key, value in extraction_rejection_counts.items()
                                            if key != "overlap_occurrence_deduplicated"),
               "extraction_overlap_duplicates_removed": extraction_rejection_counts.get("overlap_occurrence_deduplicated", 0),
               "extraction_rejection_counts": extraction_rejection_counts,
               "document_scope": document_scope,
               "graph": graph["counts"],
               "source_graph": source_graph["counts"],
               "audit_graph": audit_graph["counts"],
               "evaluation_sample": evaluation_path.name,
               "reasoning_paths": len(paths)}
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "reasoning_paths.json").write_text(json.dumps(paths, ensure_ascii=False, indent=2), encoding="utf-8")
    emit(
        "success",
        f"Finished: {len(candidates)} extracted, {len(results)} externally evaluated, "
        f"{graph['counts']['supported']} independently supported and "
        f"{graph['counts']['corroborated']} corroborated graph claims",
    )
    return summary
