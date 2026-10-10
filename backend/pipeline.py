"""Extraction, external validation, and graph construction for one PDF."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import Counter
from dataclasses import replace
from pathlib import Path
from typing import Any, Callable

from pypdf import PdfReader

from external_validator.config import ONTOLOGY_DIR, PROJECT_ROOT, SCHEMA_VERSION, Settings
from external_validator.llm import OllamaClient
from external_validator.nli import NLIValidator
from external_validator.rerank import SemanticReranker
from external_validator.storage import file_hash, stable_hash, utc_now, write_jsonl
from external_validator.workflow import validate_candidate

from .graph import graph_from_candidates, reasoning_paths, write_graphml
from .evaluation import write_evaluation_sample
from .ontology_schema import AUTOMATIC_EXTRACTION_RELATIONS, ENTITY_TYPES, schema_error
from .relevance_validation import classify_document_scope
from .semantic_validation import validate_claim_semantics
from .source_validation import validate_source_claim
from .vocabulary import normalize_entity


GENERIC_ENTITIES = {"agriculture", "food", "temperature", "yield", "disease", "soil", "pests", "crop", "health", "nutrition", "nutrient",
                    "crop growth", "crop productivity", "soil health", "soil quality", "nutrient levels",
                    "plantdisease", "soilcondition", "climatefactor", "humanexposure", "humanhealthoutcome", "agriculturalpractice"}
SPACE_RE = re.compile(r"\s+")
DOI_RE = re.compile(r"\b10\.\d{4,9}/[-._;()/:A-Z0-9]+", re.IGNORECASE)
PMCID_RE = re.compile(r"\bPMC\d{4,}\b", re.IGNORECASE)
PMID_RE = re.compile(r"\bPMID\s*[:=]?\s*(\d{5,10})\b", re.IGNORECASE)
SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“(\[])")
SECTION_HEADING_RE = re.compile(
    r"^\s*(?P<number>\d{1,2}(?:\.\d+)*)(?P<terminal_dot>\.)?\s+(?P<title>[A-Z][A-Za-z0-9 ,:;()/-]{2,140})\s*$"
)
JOURNAL_HEADER_RE = re.compile(r"^\s*Agronomy\s+\d{4},\s+\d+.*?(?:of\s+\d+)?\s*$", re.IGNORECASE)
REFERENCE_HEADING_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\.?\s+)?(?:references|bibliography)\b", re.IGNORECASE | re.MULTILINE
)
REFERENCE_ENTRY_RE = re.compile(r"(?m)^\s*(?P<number>\d{1,3})\.\s+(?P<text>.+?)(?=^\s*\d{1,3}\.\s+|\Z)", re.DOTALL)
CITATION_GROUP_RE = re.compile(
    r"\[\s*(?P<numbers>\d{1,3}(?:\s*(?:[,;]|[-\u2013\u2014\ufffd])\s*\d{1,3})*)\s*\]"
)
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
EXTRACTION_PACKET_TARGET_WORDS = 2000
EXTRACTION_PACKET_OVERLAP_WORDS = 250
EXTRACTION_MAX_CLAIMS_PER_PACKET = 4
EXTRACTION_OUTPUT_TOKENS = 1800
ESTIMATED_TOKENS_PER_WORD = 1.4
COMPARATIVE_CUE_RE = re.compile(r"\b(?:compared with|compared to|versus|vs\.?|than|relative to)\b", re.IGNORECASE)
PURPOSE_ONLY_RE = re.compile(
    r"^\s*(?:use|use of|application|adoption|deployment)\b.+?\b(?:to|for)\s+"
    r"(?:increase|reduce|control|improve|maintain|avoid|prevent)\b", re.IGNORECASE)
FIGURE_OR_TABLE_RE = re.compile(r"^\s*(?:figure|table)\s*\d", re.IGNORECASE)
NON_EMPIRICAL_RE = re.compile(r"\b(?:no information on|no evidence (?:was )?(?:available|exists)|future research)\b", re.IGNORECASE)
GENERIC_ENDPOINTS = {
    "abundance", "benefits", "crop composition", "crop quality", "food security", "health effects",
    "health impacts", "negative health effects", "negative health impacts", "nutritional quality",
    "safety parameters", "yield gap", "yield", "pest and disease", "need to produce crop calories protein",
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


def pdf_bibliography(pdf_path: Path) -> dict[int, dict[str, Any]]:
    """Parse numbered bibliography entries for citation-first retrieval.

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
    match = REFERENCE_HEADING_RE.search(text)
    if not match:
        return {}
    reference_text = text[match.end():]
    entries: dict[int, dict[str, Any]] = {}
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
    sentences: list[str] = []
    for item in re.split(r"\s*[•●▪]\s*", repaired):
        compact = SPACE_RE.sub(" ", item).strip()
        if compact:
            sentences.extend(sentence.strip() for sentence in SENTENCE_BOUNDARY_RE.split(compact) if sentence.strip())
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


def _append_section_sentences(section: dict[str, Any], paragraph: str, page: int, seen: set[str]) -> None:
    for sentence_index, sentence in enumerate(page_sentences(paragraph), 1):
        sentence_key = normalized(sentence)
        if len(re.findall(r"\w+", sentence)) < 6 or sentence_key in seen:
            continue
        seen.add(sentence_key)
        section["sentences"].append({"page": page, "page_sentence_index": sentence_index, "text": sentence})


def iter_pdf_context_packets(pdf_path: Path, max_pages: int = 0,
                             packet_target_words: int = EXTRACTION_PACKET_TARGET_WORDS,
                             overlap_words: int = EXTRACTION_PACKET_OVERLAP_WORDS):
    """Yield overlapping, section-aware passages for rich extraction context.

    Packets target roughly 2,800 model tokens while preserving sentence boundaries
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
            for line in lines:
                # Many articles use headings such as ``6. References``. Stop
                # before those citations can be mistaken for source evidence.
                if REFERENCE_HEADING_RE.match(line):
                    reference_section = True
                    break
                heading = _section_name(line)
                if heading:
                    if body_lines and current_section is not None:
                        _append_section_sentences(current_section, "\n".join(body_lines), page_index + 1, seen_sentences)
                    body_lines = []
                    current_section = {"heading": heading, "sentences": []}
                    sections.append(current_section)
                    content_started = True
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
                    continue
                if content_started:
                    body_lines.append(line)
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

    for section in sections:
        sentences = section["sentences"]
        start = 0
        packet_number = 1
        while start < len(sentences):
            end = start
            packet_words = 0
            while end < len(sentences) and (packet_words < packet_target_words or end == start):
                packet_words += word_count(sentences[end]["text"])
                end += 1
            packet = sentences[start:end]
            if not packet:
                break
            packet_word_count = sum(word_count(sentence["text"]) for sentence in packet)
            yield {
                "section_heading": section["heading"],
                "packet_number": packet_number,
                "page_start": packet[0]["page"],
                "page_end": packet[-1]["page"],
                "word_count": packet_word_count,
                "estimated_token_count": round(packet_word_count * ESTIMATED_TOKENS_PER_WORD),
                "sentences": packet,
            }
            if end >= len(sentences):
                break
            next_start = end
            overlap_count = 0
            while next_start > start + 1 and overlap_count < overlap_words:
                next_start -= 1
                overlap_count += word_count(sentences[next_start]["text"])
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


def extract_claims(pdf_path: Path, client: OllamaClient, max_pages: int,
                   emit: Callable[[str, str], None], packets: list[dict[str, Any]] | None = None,
                   document_scope: dict[str, Any] | None = None,
                   source_identifiers: dict[str, str] | None = None,
                   claims_per_packet: int = EXTRACTION_MAX_CLAIMS_PER_PACKET) -> tuple[list[dict[str, Any]], dict[str, int]]:
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
              "Use only relations affects, reduces, increases, has_nutrient, has_contaminant, contains, has_trait, higher_than, "
              "lower_than, associated_with. Do not use causes, leads_to, or deficiency_causes in automatic extraction from this "
              "review paper; those require a later, stricter causal-evidence workflow. Use has_trait only for Crop/FoodProduct -> PlantTrait. "
              "Use has_nutrient/has_contaminant/contains only for soil/crop/food material -> Nutrient or Contaminant. Use higher_than/lower_than only "
              "for an explicit comparison between crop or food materials and ALWAYS record both its metric and comparator. "
              "Do not put ontology type labels such as HumanHealthOutcome in subject or object names. "
              "Use the whole packet to understand study context, abbreviations, mechanisms, and qualifiers. "
              "For every claim, select one to three consecutive numbered evidence sentences that directly state the subject, relation, and object. "
              "subject_source_mention, object_source_mention, and relation_trigger MUST be exact consecutive words copied "
              "from those selected evidence sentences. They are mandatory proof anchors, not paraphrases. "
              "subject and object may be concise graph labels, but must not be CamelCase ontology labels or substitute a "
              "different intervention, population, material, crop, or outcome for the exact source mention. "
              "Resolve pronouns only if their antecedent is unambiguous within the selected evidence sentences. "
              "Never combine separate mechanisms into a multi-step claim. "
              "The accepted path is Agriculture/Climate -> Soil -> Crop/Disease -> Nutrient/Contaminant -> Human Exposure -> Health Outcome. "
              "A directly evidenced relation may skip a stage; do not invent intermediate entities. "
              "Exclude economics, infrastructure, trade, generic food security, and yield-only claims. "
              "Qualify entities with species, cultivar, material, comparator, exposure route, dose/condition, and outcome context "
              "whenever they constrain the finding. A result in carrots must not become a universal AMF claim; a cultivar comparison "
              "must retain both cultivar names and the metric. Skip locations, spatial patterns, maps, figures, tables, captions, "
              "section outlines, recommendations (for example 'use X to increase Y'), model-performance statistics, methods, "
              "measurements, generic background claims, and inferred links. "
              "Do not discard a directly reported hedged relation: preserve may/can/thought/suggested wording as conditional certainty. "
              "Use causal certainty only for an unhedged explicit causal statement; increases/reduces may be descriptive findings. "
              f"Return at most {claims_per_packet} distinct, high-value eligible claims supported by a local evidence span; do not try to cover every sentence. "
              "There is no target claim count; zero is better than a weak claim. Do not repeat the evidence text in another field; sentence numbers are sufficient. "
              "A sentence saying linked or associated supports only associated_with, never causes, increases, or reduces. "
              "If no claim has a direct one-to-three-sentence proof span, return {\"claims\":[]}.")
    candidates: list[dict[str, Any]] = []
    seen_occurrences: dict[tuple[Any, ...], dict[str, Any]] = {}
    rejected: Counter[str] = Counter()
    bibliography = pdf_bibliography(pdf_path)
    response_schema = json.loads(json.dumps(EXTRACTION_RESPONSE_SCHEMA))
    response_schema["properties"]["claims"]["maxItems"] = claims_per_packet
    packets = packets if packets is not None else list(iter_pdf_context_packets(pdf_path, max_pages))
    if not packets:
        raise ValueError("The PDF has no extractable text. OCR is required before extraction.")
    for index, packet in enumerate(packets, 1):
        packet_sentences = packet["sentences"]
        packet_text = "\n".join(f"[{number}] {sentence['text']}" for number, sentence in enumerate(packet_sentences, 1))
        emit("info", f"Extracting {packet['section_heading']} (pages {packet['page_start']}-{packet['page_end']}), packet {packet['packet_number']} ({index}/{len(packets)}; {packet['word_count']} words, ~{packet['estimated_token_count']} tokens)")
        try:
            result = client.chat_json(system, f"Source paper: {source_title}\n"
                                      f"Section: {packet['section_heading']}\n"
                                      f"Pages: {packet['page_start']}-{packet['page_end']}\n"
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
                         "certainty": str(item.get("certainty", "")).strip().lower()}
            raw_qualifiers = item.get("qualifiers")
            candidate["qualifiers"] = {
                key: str(raw_qualifiers.get(key, "")).strip()[:160]
                for key in ("crop_or_species", "population", "condition", "comparator", "metric")
            } if isinstance(raw_qualifiers, dict) else {}
            evidence = evidence_span_from_item(item, packet_sentences)
            if not subject or not obj or evidence is None or "," in subject or "," in obj:
                rejected["malformed"] += 1
                continue
            if normalized(subject) in GENERIC_ENTITIES or normalized(obj) in GENERIC_ENTITIES:
                rejected["generic_entity"] += 1
                continue
            evidence_numbers, evidence_sentences, evidence_span = evidence
            shape_error = extraction_shape_error(candidate, evidence_span)
            if shape_error:
                rejected[shape_error] += 1
                continue
            subject_source_mention = source_surface_mention(evidence_span, proposed_subject_mention)
            object_source_mention = source_surface_mention(evidence_span, proposed_object_mention)
            relation_trigger = source_surface_mention(evidence_span, proposed_relation_trigger)
            if not subject_source_mention or not object_source_mention or not relation_trigger:
                rejected["unanchored_evidence"] += 1
                continue
            evidence_page = evidence_sentences[0]["page"]
            cited_numbers = citation_numbers(evidence_span)
            cited_references = [bibliography[number] for number in cited_numbers if number in bibliography]
            evidence_record = {
                "page": evidence_page,
                "page_start": packet["page_start"],
                "page_end": packet["page_end"],
                "section_heading": packet["section_heading"],
                "sentence_numbers": evidence_numbers,
                "evidence_span": evidence_span,
                "subject_source_mention": subject_source_mention,
                "object_source_mention": object_source_mention,
                "relation_trigger": relation_trigger,
                "certainty": candidate["certainty"],
                "subject_type": candidate["subject_type"],
                "object_type": candidate["object_type"],
                "qualifiers": dict(candidate["qualifiers"]),
                "citation_numbers": cited_numbers,
                "references": cited_references,
            }
            # Only identical occurrences introduced by overlapping packets are
            # deduplicated. Different source spans, types, or modalities remain
            # separate assertions and pass semantic/source validation on their
            # own before any later graph-level aggregation.
            key = (normalized(subject), relation, normalized(obj), candidate["certainty"],
                   candidate["subject_type"], candidate["object_type"], evidence_page,
                   normalized(evidence_span),
                   tuple((name, normalized(value)) for name, value in sorted(candidate["qualifiers"].items())))
            if key in seen_occurrences:
                existing = seen_occurrences[key]
                existing.setdefault("source_evidence_records", []).append(evidence_record)
                existing["source_citation_numbers"] = sorted(set(existing.get("source_citation_numbers", [])) | set(cited_numbers))
                known = {item["reference_number"] for item in existing.get("source_references", [])}
                existing.setdefault("source_references", []).extend(item for item in cited_references if item["reference_number"] not in known)
                rejected["overlap_occurrence_deduplicated"] += 1
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
                              "source_section_heading": packet["section_heading"], "source_packet_number": packet["packet_number"],
                              "source_context_estimated_tokens": packet["estimated_token_count"],
                              "source_evidence_sentence_numbers": evidence_numbers,
                              "source_evidence_span": evidence_span,
                              "subject_source_mention": subject_source_mention,
                              "object_source_mention": object_source_mention,
                              "relation_trigger": relation_trigger,
                              "source_citation_numbers": cited_numbers,
                              "source_references": cited_references,
                              "source_evidence_records": [evidence_record],
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
    return candidates, dict(rejected)


def run_pipeline(pdf_path: Path, max_pages: int, max_claims: int, articles_per_claim: int,
                 emit: Callable[[str, str], None], model: str | None = None,
                 claims_per_packet: int | None = None) -> dict[str, Any]:
    output_dir = ONTOLOGY_DIR / pdf_path.stem
    output_dir.mkdir(parents=True, exist_ok=True)
    settings = replace(Settings(), model=model) if model else Settings()
    claims_per_packet = claims_per_packet or settings.extraction_claims_per_packet
    if not 1 <= claims_per_packet <= 8:
        raise ValueError("claims_per_packet must be between 1 and 8")
    client = OllamaClient(settings)
    names = [model.get("name") for model in client.health_check().get("models", [])]
    if settings.model not in names:
        raise RuntimeError(f"Local Ollama model {settings.model} is unavailable")
    emit("success", f"Connected to local model {settings.model}")
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
            "model": settings.model,
            "completed_at": utc_now(),
            "run_status": "blocked_text_quality",
            "schema_version": SCHEMA_VERSION,
            "claims_per_packet": claims_per_packet,
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
        emit("warning", "Skipped extraction: the document is outside the soil-to-human-health evidence scope")
        candidates, extraction_rejection_counts = [], {"document_out_of_scope": 1}
    else:
        if document_scope["scope"] == "upstream_only":
            emit("info", "Paper is upstream-only; retained for evidence-backed pathway bridges, not treated as a health-outcome paper")
        emit("stage", "Extracting evidence-anchored claims from PDF")
        candidates, extraction_rejection_counts = extract_claims(pdf_path, client, max_pages, emit,
                                                                  packets=packets, document_scope=document_scope,
                                                                  source_identifiers=source_identifiers,
                                                                  claims_per_packet=claims_per_packet)
    write_jsonl(output_dir / "candidates.jsonl", candidates)
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
    nli_validator: NLIValidator | None = None
    if semantic_accepted and settings.enable_nli:
        emit("info", f"Loading independent NLI validator {settings.nli_model} on {settings.nli_device}")
        try:
            nli_validator = NLIValidator(settings)
            nli_validator.warmup()
            emit("success", "Independent NLI validator ready")
        except Exception as exc:
            nli_validator = None
            emit("warning", f"NLI validator unavailable; exact anchors and Qwen checks remain active: {exc}")
    emit("stage", f"Checking source evidence for {len(semantic_accepted)} claims")
    source_results: list[dict[str, Any]] = []
    write_jsonl(output_dir / "source_validation.jsonl", source_results)
    for position, candidate in enumerate(semantic_accepted, 1):
        emit("info", f"Source claim {position}/{len(semantic_accepted)}: {candidate['subject']} → {candidate['relation']} → {candidate['object']}")
        try:
            source_result = validate_source_claim(candidate, client, nli_validator=nli_validator)
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
    emit("stage", f"Validating {len(selected)} claims against Europe PMC")
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
    summary = {"pdf": pdf_path.name, "pdf_sha256": file_hash(pdf_path), "model": settings.model, "completed_at": utc_now(),
               "run_status": "completed",
               "schema_version": SCHEMA_VERSION,
               "claims_per_packet": claims_per_packet,
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
    emit("success", f"Finished: {len(candidates)} extracted, {len(results)} externally evaluated, {graph['counts']['corroborated']} corroborated graph claims")
    return summary
