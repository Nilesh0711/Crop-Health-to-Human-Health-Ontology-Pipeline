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
from external_validator.rerank import SemanticReranker
from external_validator.storage import file_hash, stable_hash, utc_now, write_jsonl
from external_validator.workflow import validate_candidate

from .graph import graph_from_candidates, reasoning_paths, write_graphml
from .ontology_schema import ENTITY_TYPES, RELATIONS, schema_error
from .semantic_validation import validate_claim_semantics
from .source_validation import validate_source_claim


GENERIC_ENTITIES = {"agriculture", "food", "temperature", "yield", "disease", "soil", "pests", "crop", "health", "nutrition", "nutrient",
                    "crop growth", "crop productivity", "soil health", "soil quality", "nutrient levels",
                    "plantdisease", "soilcondition", "climatefactor", "humanexposure", "humanhealthoutcome", "agriculturalpractice"}
SPACE_RE = re.compile(r"\s+")
SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9\"'“(\[])")
SECTION_HEADING_RE = re.compile(r"^\s*\d+(?:\.\d+)*\.\s+[A-Z][A-Za-z0-9 ,:;()/-]{2,140}\s*$")
JOURNAL_HEADER_RE = re.compile(r"^\s*Agronomy\s+\d{4},\s+\d+.*?(?:of\s+\d+)?\s*$", re.IGNORECASE)
EXTRACTION_PACKET_TARGET_WORDS = 2000
EXTRACTION_PACKET_OVERLAP_WORDS = 250
EXTRACTION_MAX_CLAIMS_PER_PACKET = 10
EXTRACTION_OUTPUT_TOKENS = 2600
ESTIMATED_TOKENS_PER_WORD = 1.4

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
                    "relation": {"type": "string", "enum": sorted(RELATIONS)},
                    "object": {"type": "string"},
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
                        },
                        "required": ["crop_or_species", "population", "condition"],
                        "additionalProperties": False,
                    },
                },
                "required": ["subject", "relation", "object", "subject_type", "object_type",
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


def anchored_quote(source_text: str, proposed_quote: str) -> str | None:
    """Return the exact extracted-text span when all quote words occur in order."""
    words = re.findall(r"\w+", unicodedata.normalize("NFKC", proposed_quote))
    if len(words) < 5:
        return None
    pattern = r"(?<!\w)" + r"\W+".join(re.escape(word) for word in words) + r"(?!\w)"
    match = re.search(pattern, source_text, flags=re.IGNORECASE)
    return source_text[match.start():match.end()] if match else None


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
    if match and int(compact.split(".", 1)[0]) <= 20:
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
                if line.casefold() in {"references", "bibliography"}:
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
            before_references = re.split(r"\n\s*(?:references|bibliography)\s*\n", text, maxsplit=1, flags=re.IGNORECASE)[0]
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
                   emit: Callable[[str, str], None]) -> tuple[list[dict[str, Any]], dict[str, int]]:
    digest = file_hash(pdf_path)
    document_id = "local-" + stable_hash(f"{pdf_path.stem}|{digest}", 32)
    metadata = PdfReader(str(pdf_path)).metadata
    metadata_title = str(metadata.title or "").strip() if metadata else ""
    source_title = metadata_title if 12 <= len(metadata_title) <= 250 else pdf_path.stem
    system = ("Extract only nontrivial, directly stated scientific relations from the supplied SECTION CONTEXT PACKET. "
              "Return JSON {\"claims\":[{\"subject\":...,\"relation\":...,\"object\":...,\"subject_type\":...,"
              "\"object_type\":...,\"evidence_sentence_numbers\":[1],"
              "\"certainty\":\"causal|associative|conditional|descriptive\",\"qualifiers\":{\"crop_or_species\":\"\",\"population\":\"\",\"condition\":\"\"}}]}. "
              "Use only these entity types: AgriculturalPractice (management intervention, not an organism), ClimateFactor, "
              "SoilCondition (abiotic soil state or process), SoilBiota (living soil microbes and fungi), Crop (plant or edible crop material), "
              "PlantDisease (named infection or disease), PlantPest (pest organism), PlantProcess (plant uptake or biological process), "
              "PlantTrait (crop composition, quality, resistance level or other measurable characteristic), "
              "Nutrient (beneficial chemical or content), Contaminant (harmful chemical or mycotoxin), HumanExposure (intake/contact), "
              "HumanBiologicalProcess (human biomarker, immune, metabolic or epigenetic process), "
              "HumanHealthOutcome (human disease or health effect, never crop yield). "
              "Use only relations affects, causes, reduces, increases, has_nutrient, has_contaminant, leads_to, associated_with, deficiency_causes. "
              "Do not put ontology type labels such as HumanHealthOutcome in subject or object names. "
              "Use the whole packet to understand study context, abbreviations, mechanisms, and qualifiers. "
              "For every claim, select one to three consecutive numbered evidence sentences that directly state the subject, relation, and object. "
              "Resolve pronouns only if their antecedent is unambiguous within the selected evidence sentences. "
              "Never combine separate mechanisms into a multi-step claim. "
              "The accepted path is Agriculture/Climate -> Soil -> Crop/Disease -> Nutrient/Contaminant -> Human Exposure -> Health Outcome. "
              "A directly evidenced relation may skip a stage; do not invent intermediate entities. "
              "Exclude economics, infrastructure, trade, generic food security, and yield-only claims. "
              "Qualify entities with species, material, exposure, and outcome context. Skip locations, spatial patterns, methods, measurements, generic background claims, hedged causality, and inferred links. "
              "Return at most 10 distinct, high-value eligible claims supported by a local evidence span; do not try to cover every sentence. "
              "There is no target claim count; zero is better than a weak claim. Do not repeat the evidence text in another field; sentence numbers are sufficient. "
              "A sentence saying linked or associated supports only associated_with, never causes, increases, or reduces. "
              "If no claim has a direct one-to-three-sentence proof span, return {\"claims\":[]}.")
    candidates: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    rejected: Counter[str] = Counter()
    packets = list(iter_pdf_context_packets(pdf_path, max_pages))
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
                                      json_schema=EXTRACTION_RESPONSE_SCHEMA)
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
            relation = str(item.get("relation", "")).strip().lower()
            obj = str(item.get("object", "")).strip()
            candidate = {"subject": subject, "relation": relation, "object": obj,
                         "subject_type": str(item.get("subject_type", "")).strip(),
                         "object_type": str(item.get("object_type", "")).strip(),
                         "certainty": str(item.get("certainty", "")).strip().lower()}
            raw_qualifiers = item.get("qualifiers")
            candidate["qualifiers"] = {
                key: str(raw_qualifiers.get(key, "")).strip()[:160]
                for key in ("crop_or_species", "population", "condition")
            } if isinstance(raw_qualifiers, dict) else {}
            evidence = evidence_span_from_item(item, packet_sentences)
            if not subject or not obj or evidence is None or "," in subject or "," in obj:
                rejected["malformed"] += 1
                continue
            if normalized(subject) in GENERIC_ENTITIES or normalized(obj) in GENERIC_ENTITIES:
                rejected["generic_entity"] += 1
                continue
            evidence_numbers, evidence_sentences, evidence_span = evidence
            # Sentence numbers are the authoritative provenance. The exact PDF
            # span is derived here instead of asking Qwen to repeat it.
            key = (normalized(subject), relation, normalized(obj), normalized(evidence_span))
            if key in seen:
                rejected["duplicate"] += 1
                continue
            seen.add(key)
            evidence_page = evidence_sentences[0]["page"]
            candidate_id = "candidate-" + stable_hash(f"{document_id}|{evidence_page}|{subject}|{relation}|{obj}|{evidence_span}", 32)
            candidate.update({"schema_version": SCHEMA_VERSION, "candidate_id": candidate_id,
                              "source_document_id": document_id, "source_paper_title": source_title,
                              "source_pdf_sha256": digest, "source_page": evidence_page,
                              "source_page_start": packet["page_start"], "source_page_end": packet["page_end"],
                              "source_section_heading": packet["section_heading"], "source_packet_number": packet["packet_number"],
                              "source_context_estimated_tokens": packet["estimated_token_count"],
                              "source_evidence_sentence_numbers": evidence_numbers,
                              "source_evidence_span": evidence_span,
                              "source_context_packet": " ".join(sentence["text"] for sentence in packet_sentences),
                              })
            candidate["schema_error"] = schema_error(candidate)
            candidates.append(candidate)
        emit("success", f"{packet['section_heading']}: {len(candidates)} accepted candidates so far")
    emit("info", f"Extraction complete: {len(candidates)} candidates; {sum(rejected.values())} discarded ({dict(rejected)})")
    return candidates, dict(rejected)


def run_pipeline(pdf_path: Path, max_pages: int, max_claims: int, articles_per_claim: int,
                 emit: Callable[[str, str], None], model: str | None = None) -> dict[str, Any]:
    output_dir = ONTOLOGY_DIR / pdf_path.stem
    output_dir.mkdir(parents=True, exist_ok=True)
    settings = replace(Settings(), model=model) if model else Settings()
    client = OllamaClient(settings)
    names = [model.get("name") for model in client.health_check().get("models", [])]
    if settings.model not in names:
        raise RuntimeError(f"Local Ollama model {settings.model} is unavailable")
    emit("success", f"Connected to local model {settings.model}")
    emit("stage", "Extracting claims from PDF")
    candidates, extraction_rejection_counts = extract_claims(pdf_path, client, max_pages, emit)
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
    initial_audit_graph = graph_from_candidates(candidates, results, view="audit")
    (output_dir / "graph.json").write_text(json.dumps(initial_graph, ensure_ascii=False, indent=2), encoding="utf-8")
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
                                        progress=lambda message: emit("info", message))
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
    audit_graph = graph_from_candidates(candidates, results, view="audit")
    (output_dir / "graph.json").write_text(json.dumps(graph, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "audit_graph.json").write_text(json.dumps(audit_graph, ensure_ascii=False, indent=2), encoding="utf-8")
    write_graphml(graph, output_dir / "knowledge_graph.graphml")
    paths = reasoning_paths(graph)
    summary = {"pdf": pdf_path.name, "pdf_sha256": file_hash(pdf_path), "model": settings.model, "completed_at": utc_now(),
               "extracted": len(candidates), "schema_valid": len(structurally_valid),
               "semantic_accepted": len(semantic_accepted),
               "semantic_needs_review": sum(item["decision"] == "needs_review" for item in semantic_results),
               "semantic_rejected": sum(item["decision"] == "rejected" for item in semantic_results),
               "source_entailed": len(source_entailed),
               "source_needs_review": sum(item["decision"] == "needs_review" for item in source_results),
               "source_unsupported": sum(item["decision"] == "unsupported" for item in source_results),
               "externally_evaluated": len(results),
               "extraction_rejections": sum(extraction_rejection_counts.values()),
               "extraction_rejection_counts": extraction_rejection_counts,
               "graph": graph["counts"],
               "audit_graph": audit_graph["counts"],
               "reasoning_paths": len(paths)}
    (output_dir / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output_dir / "reasoning_paths.json").write_text(json.dumps(paths, ensure_ascii=False, indent=2), encoding="utf-8")
    emit("success", f"Finished: {len(candidates)} extracted, {len(results)} externally evaluated, {graph['counts']['corroborated']} corroborated graph claims")
    return summary
