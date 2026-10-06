"""External evidence retrieval for knowledge-graph candidate triples.

This script deliberately does *not* build a local mirror of Europe PMC or PMC.
It creates source-aware candidate records, retrieves a small, claim-specific set
of open-access external articles, extracts only the best evidence chunks in
memory, and persists those chunks plus article metadata as validation packets.

The script does not perform source-paper entailment.  A source paper is kept as
candidate provenance only; its text is not used as validation evidence here.

Commands:
    python external_validation.py build-candidates
    python external_validation.py retrieve --limit 5 --articles-per-candidate 5
    python external_validation.py ingest-decisions rag/entailment_decisions.jsonl

`retrieve` makes live requests to Europe PMC.  It writes no external PDF, XML,
or full article text to disk.  Full text is parsed in memory and discarded once
the selected chunks have been written to a validation packet.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


PROJECT_ROOT = Path(__file__).resolve().parent
ONTOLOGY_DIR = PROJECT_ROOT / "ontology"
PDF_DIR = PROJECT_ROOT / "pdf"
RAG_DIR = PROJECT_ROOT / "rag"

EUROPE_PMC_SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
EUROPE_PMC_FULLTEXT_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{source}/{article_id}/fullTextXML"
EUROPE_PMC_FULLTEXT_BY_ID_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{article_id}/fullTextXML"
USER_AGENT = "crop-to-human-health-antology/0.1 (external evidence validation)"
MAX_FULLTEXT_BYTES = 8 * 1024 * 1024
SCHEMA_VERSION = "external-validation-v1"

TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9+._/-]*")
WHITESPACE_RE = re.compile(r"\s+")

RELATION_QUERY_TERMS = {
    "affects": ["affect", "impact", "influence"],
    "associated_with": ["associated", "association", "correlation", "linked"],
    "causes": ["cause", "causal", "induce", "induced"],
    "deficiency_causes": ["deficiency", "deficient", "insufficiency"],
    "has_nutrient": ["contains", "content", "composition"],
    "increases": ["increase", "increased", "elevated", "raise"],
    "leads_to": ["lead", "consequence", "result"],
    "reduces": ["reduce", "reduced", "decrease", "lower"],
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def stable_hash(value: str, length: int = 24) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:length]


def file_hash(path: Path) -> str | None:
    if not path.exists():
        return None
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def jsonl_records(path: Path) -> Iterable[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"Invalid JSONL in {path} line {line_number}: {exc}") from exc
            if not isinstance(record, dict):
                raise ValueError(f"Expected a JSON object in {path} line {line_number}")
            records.append(record)
    return records


def write_jsonl(path: Path, records: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for record in records:
            handle.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
            handle.write("\n")


def parse_tuple_line(line: str) -> dict[str, Any] | None:
    """Parse the existing six-tuple format without applying ontology rules."""
    line = line.strip()
    if not line.startswith("<") or not line.endswith(">"):
        return None
    parts = [part.strip() for part in line[1:-1].split(",")]
    if len(parts) != 6 or not all(parts[:5]):
        return None
    subject, relation, obj, subject_type, object_type, confidence = parts
    try:
        extraction_confidence: float | None = float(confidence)
    except ValueError:
        extraction_confidence = None
    return {
        "subject": subject,
        "relation": relation.lower(),
        "object": obj,
        "subject_type": subject_type,
        "object_type": object_type,
        "extraction_confidence": extraction_confidence,
    }


def find_source_pdf(paper_name: str) -> Path | None:
    exact_path = PDF_DIR / f"{paper_name}.pdf"
    if exact_path.exists():
        return exact_path
    normalized = paper_name.casefold()
    for path in PDF_DIR.glob("*.pdf"):
        if path.stem.casefold() == normalized:
            return path
    return None


def build_candidates(tuple_filename: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    """Create candidates and source metadata from existing per-paper tuples."""
    candidates: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []

    for paper_dir in sorted(path for path in ONTOLOGY_DIR.iterdir() if path.is_dir()):
        tuple_path = paper_dir / tuple_filename
        if not tuple_path.exists():
            continue
        pdf_path = find_source_pdf(paper_dir.name)
        pdf_digest = file_hash(pdf_path) if pdf_path else None
        source_document_id = "local-" + stable_hash(
            f"{paper_dir.name}|{pdf_digest or 'missing-pdf'}", 32
        )
        documents.append(
            {
                "schema_version": SCHEMA_VERSION,
                "document_id": source_document_id,
                "document_role": "candidate_origin",
                "title": paper_dir.name,
                "local_pdf_path": str(pdf_path.relative_to(PROJECT_ROOT)) if pdf_path else None,
                "file_sha256": pdf_digest,
                "source_tuple_file": str(tuple_path.relative_to(PROJECT_ROOT)),
                "created_at": utc_now(),
            }
        )
        for line_number, raw_line in enumerate(tuple_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not raw_line.strip():
                continue
            parsed = parse_tuple_line(raw_line)
            if parsed is None:
                parse_errors.append(
                    {
                        "schema_version": SCHEMA_VERSION,
                        "source_document_id": source_document_id,
                        "source_tuple_file": str(tuple_path.relative_to(PROJECT_ROOT)),
                        "line_number": line_number,
                        "raw_tuple": raw_line,
                        "error": "Expected a six-field angle-bracket tuple",
                    }
                )
                continue
            candidate_key = f"{source_document_id}|{line_number}|{raw_line.strip()}"
            candidates.append(
                {
                    "schema_version": SCHEMA_VERSION,
                    "candidate_id": "candidate-" + stable_hash(candidate_key, 32),
                    "source_document_id": source_document_id,
                    "source_paper_title": paper_dir.name,
                    "source_tuple_file": str(tuple_path.relative_to(PROJECT_ROOT)),
                    "source_tuple_line": line_number,
                    "raw_tuple": raw_line.strip(),
                    **parsed,
                    "candidate_status": "pending_external_retrieval",
                    "created_at": utc_now(),
                }
            )
    return candidates, documents, parse_errors


def tokens(value: str) -> set[str]:
    return {match.group(0).casefold() for match in TOKEN_RE.finditer(value) if len(match.group(0)) > 1}


def europe_pmc_query(candidate: dict[str, Any]) -> str:
    """Build a transparent broad query; ranking happens on the returned text."""
    subject = candidate["subject"].replace('"', " ")
    obj = candidate["object"].replace('"', " ")
    relation_terms = RELATION_QUERY_TERMS.get(candidate["relation"], [])[:3]
    relation_query = " OR ".join(relation_terms)
    if relation_query:
        return f'("{subject}") AND ("{obj}") AND ({relation_query})'
    return f'("{subject}") AND ("{obj}")'


def fetch_json(url: str, timeout: int = 30) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = response.read(MAX_FULLTEXT_BYTES)
    return json.loads(payload.decode("utf-8"))


def fetch_fulltext_xml(source: str, article_id: str, timeout: int = 45) -> str | None:
    """Fetch external full text into memory only; never persist XML or a PDF."""
    encoded_id = urllib.parse.quote(article_id, safe="")
    urls = [
        EUROPE_PMC_FULLTEXT_URL.format(source=urllib.parse.quote(source, safe=""), article_id=encoded_id),
        EUROPE_PMC_FULLTEXT_BY_ID_URL.format(article_id=encoded_id),
    ]
    for url in urls:
        request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/xml,text/xml"})
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                payload = response.read(MAX_FULLTEXT_BYTES + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            continue
        if len(payload) <= MAX_FULLTEXT_BYTES:
            return payload.decode("utf-8", errors="replace")
    return None


def is_open_access(result: dict[str, Any]) -> bool:
    return str(result.get("isOpenAccess", "")).upper() in {"Y", "YES", "TRUE", "1"}


def fulltext_identifiers(result: dict[str, Any]) -> list[tuple[str, str]]:
    """Return likely Europe PMC full-text endpoints, most specific first."""
    identifiers: list[tuple[str, str]] = []
    pmcid = result.get("pmcid")
    if pmcid:
        identifiers.append(("PMC", str(pmcid)))
    source, article_id = result.get("source"), result.get("id")
    if source and article_id:
        identifiers.append((str(source), str(article_id)))
    return list(dict.fromkeys(identifiers))


def normalize_text(value: str) -> str:
    return WHITESPACE_RE.sub(" ", value).strip()


def extract_paragraphs(xml_text: str) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    paragraphs: list[str] = []
    for element in root.findall(".//abstract//p") + root.findall(".//body//p"):
        text = normalize_text(" ".join(element.itertext()))
        if len(text) >= 80:
            paragraphs.append(text)
    return paragraphs


def make_chunks(paragraphs: list[str], size: int = 320, overlap: int = 60) -> list[str]:
    chunks: list[str] = []
    for paragraph in paragraphs:
        words = paragraph.split()
        if len(words) <= size:
            chunks.append(paragraph)
            continue
        step = max(1, size - overlap)
        for start in range(0, len(words), step):
            part = words[start : start + size]
            if len(part) < 40:
                break
            chunks.append(" ".join(part))
    return chunks


def chunk_score(candidate: dict[str, Any], chunk: str) -> int:
    chunk_tokens = tokens(chunk)
    subject_tokens = tokens(candidate["subject"])
    object_tokens = tokens(candidate["object"])
    relation_tokens = set(RELATION_QUERY_TERMS.get(candidate["relation"], []))
    # Entity matches matter more than a generic relation verb.
    score = 3 * len(subject_tokens & chunk_tokens) + 3 * len(object_tokens & chunk_tokens)
    score += len(relation_tokens & chunk_tokens)
    subject_phrase = candidate["subject"].casefold()
    object_phrase = candidate["object"].casefold()
    lowered = chunk.casefold()
    if subject_phrase in lowered:
        score += 4
    if object_phrase in lowered:
        score += 4
    return score


def selected_evidence_chunks(candidate: dict[str, Any], article_key: str, xml_text: str, top_chunks: int) -> list[dict[str, Any]]:
    ranked = sorted(
        (
            (chunk_score(candidate, chunk), index, chunk)
            for index, chunk in enumerate(make_chunks(extract_paragraphs(xml_text)))
        ),
        reverse=True,
    )
    selected: list[dict[str, Any]] = []
    for score, index, chunk in ranked:
        if score <= 0 or len(selected) >= top_chunks:
            break
        selected.append(
            {
                "chunk_id": f"{article_key}:chunk-{index}",
                "rank_score": score,
                "text": chunk,
            }
        )
    return selected


def article_metadata(result: dict[str, Any], article_key: str) -> dict[str, Any]:
    return {
        "evidence_document_id": article_key,
        "source": result.get("source"),
        "source_id": result.get("id"),
        "pmid": result.get("pmid"),
        "pmcid": result.get("pmcid"),
        "doi": result.get("doi"),
        "title": result.get("title"),
        "journal": result.get("journalTitle"),
        "publication_year": result.get("pubYear"),
        "author_string": result.get("authorString"),
        "is_open_access": is_open_access(result),
    }


def retrieve_external_evidence(candidate: dict[str, Any], articles_per_candidate: int, top_chunks: int) -> dict[str, Any]:
    query = europe_pmc_query(candidate)
    params = urllib.parse.urlencode(
        {"query": query, "format": "json", "pageSize": max(articles_per_candidate * 3, 10), "resultType": "core"}
    )
    response = fetch_json(f"{EUROPE_PMC_SEARCH_URL}?{params}")
    results = response.get("resultList", {}).get("result", [])
    evidence_documents: list[dict[str, Any]] = []
    retrieval_notes: list[dict[str, str]] = []
    seen_articles: set[str] = set()

    for result in results:
        if len(evidence_documents) >= articles_per_candidate:
            break
        if not is_open_access(result):
            continue
        endpoint_pairs = fulltext_identifiers(result)
        if not endpoint_pairs:
            continue
        article_key = "external-" + stable_hash(
            f"{result.get('source')}|{result.get('id')}|{result.get('pmcid')}|{result.get('doi')}", 32
        )
        if article_key in seen_articles:
            continue
        seen_articles.add(article_key)

        xml_text = None
        for source, article_id in endpoint_pairs:
            xml_text = fetch_fulltext_xml(source, article_id)
            if xml_text:
                break
        if not xml_text:
            retrieval_notes.append({"article_id": article_key, "reason": "full_text_unavailable_or_not_permitted"})
            continue

        chunks = selected_evidence_chunks(candidate, article_key, xml_text, top_chunks)
        # xml_text falls out of scope after this function; only selected chunks persist.
        if not chunks:
            retrieval_notes.append({"article_id": article_key, "reason": "no_relevant_chunk"})
            continue
        evidence_documents.append({**article_metadata(result, article_key), "evidence_chunks": chunks})

    return {
        "schema_version": SCHEMA_VERSION,
        "candidate_id": candidate["candidate_id"],
        "candidate": candidate,
        "retrieval": {
            "provider": "Europe PMC",
            "query": query,
            "result_count": len(results),
            "open_access_evidence_documents": len(evidence_documents),
            "notes": retrieval_notes,
            "retrieved_at": utc_now(),
        },
        "evidence_documents": evidence_documents,
        "validation_status": "pending_per_document_entailment",
    }


def load_candidates(path: Path) -> list[dict[str, Any]]:
    return list(jsonl_records(path))


def decision_errors(packet: dict[str, Any], decision: dict[str, Any]) -> list[str]:
    """Reject decisions that cannot be traced to stored external evidence."""
    errors: list[str] = []
    document_id = decision.get("evidence_document_id")
    evidence_document = next(
        (document for document in packet.get("evidence_documents", []) if document.get("evidence_document_id") == document_id),
        None,
    )
    if evidence_document is None:
        return ["evidence_document_id is not present in this candidate's validation packet"]
    allowed_decisions = {"supported", "contradicted", "insufficient", "needs_review"}
    if decision.get("decision") not in allowed_decisions:
        errors.append("decision must be supported, contradicted, insufficient, or needs_review")
    evidence = decision.get("evidence", [])
    if not isinstance(evidence, list):
        errors.append("evidence must be a list")
        return errors
    if decision.get("decision") in {"supported", "contradicted"} and not evidence:
        errors.append("supported/contradicted decisions require at least one cited evidence chunk")
    chunks = {chunk["chunk_id"]: chunk["text"] for chunk in evidence_document.get("evidence_chunks", [])}
    for item in evidence:
        if not isinstance(item, dict):
            errors.append("each evidence item must be an object")
            continue
        chunk_text = chunks.get(item.get("chunk_id"))
        quote = normalize_text(str(item.get("quoted_span", "")))
        if chunk_text is None:
            errors.append("evidence chunk_id is not present in the cited external document")
        elif not quote:
            errors.append("evidence item lacks quoted_span")
        elif quote.casefold() not in normalize_text(chunk_text).casefold():
            errors.append("quoted_span is not contained in the cited stored evidence chunk")
    return errors


def command_build_candidates(args: argparse.Namespace) -> int:
    candidates, documents, parse_errors = build_candidates(args.tuple_file)
    write_jsonl(RAG_DIR / "candidates.jsonl", candidates)
    write_jsonl(RAG_DIR / "documents.jsonl", documents)
    write_jsonl(RAG_DIR / "candidate_parse_errors.jsonl", parse_errors)
    print(f"Wrote {len(candidates)} candidates and {len(documents)} source metadata records to {RAG_DIR}")
    if parse_errors:
        print(f"Recorded {len(parse_errors)} malformed tuple lines in candidate_parse_errors.jsonl")
    return 0


def command_retrieve(args: argparse.Namespace) -> int:
    candidates_path = RAG_DIR / "candidates.jsonl"
    if not candidates_path.exists():
        print("candidates.jsonl not found. Run build-candidates first.", file=sys.stderr)
        return 2
    candidates = load_candidates(candidates_path)
    if args.candidate_id:
        candidates = [candidate for candidate in candidates if candidate["candidate_id"] == args.candidate_id]
    if args.limit is not None:
        candidates = candidates[: args.limit]
    if not candidates:
        print("No candidates selected.", file=sys.stderr)
        return 2

    packets: list[dict[str, Any]] = []
    for position, candidate in enumerate(candidates, start=1):
        print(f"[{position}/{len(candidates)}] Retrieving external evidence for {candidate['candidate_id']}")
        try:
            packet = retrieve_external_evidence(candidate, args.articles_per_candidate, args.top_chunks)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            packet = {
                "schema_version": SCHEMA_VERSION,
                "candidate_id": candidate["candidate_id"],
                "candidate": candidate,
                "evidence_documents": [],
                "validation_status": "retrieval_failed",
                "retrieval_error": str(exc),
                "retrieved_at": utc_now(),
            }
        packets.append(packet)
        if args.delay_seconds and position < len(candidates):
            time.sleep(args.delay_seconds)

    write_jsonl(RAG_DIR / "external_validation_packets.jsonl", packets)
    evidence_count = sum(len(packet["evidence_documents"]) for packet in packets)
    print(f"Wrote {len(packets)} validation packets with {evidence_count} external evidence documents.")
    print("Only selected evidence chunks and article metadata were persisted; full external text was discarded.")
    return 0


def command_ingest_decisions(args: argparse.Namespace) -> int:
    packets_path = RAG_DIR / "external_validation_packets.jsonl"
    if not packets_path.exists():
        print("external_validation_packets.jsonl not found. Run retrieve first.", file=sys.stderr)
        return 2
    packets = {packet["candidate_id"]: packet for packet in jsonl_records(packets_path)}
    decisions = list(jsonl_records(Path(args.decisions)))
    decisions_by_candidate: dict[str, list[dict[str, Any]]] = {}
    invalid_decisions: list[dict[str, Any]] = []
    for decision in decisions:
        candidate_id = decision.get("candidate_id")
        if candidate_id not in packets:
            invalid_decisions.append({"decision": decision, "errors": ["candidate_id is not present in validation packets"]})
            continue
        errors = decision_errors(packets[candidate_id], decision)
        if errors:
            invalid_decisions.append({"decision": decision, "errors": errors})
            continue
        decisions_by_candidate.setdefault(candidate_id, []).append(decision)

    ledger: list[dict[str, Any]] = []
    for candidate_id, packet in packets.items():
        candidate_decisions = decisions_by_candidate.get(candidate_id, [])
        counts = Counter(str(decision.get("decision", "needs_review")) for decision in candidate_decisions)
        if counts["supported"] and counts["contradicted"]:
            status = "world_contested"
        elif counts["supported"] >= 2:
            status = "candidate_for_corroboration_review"
        elif counts["supported"] == 1:
            status = "world_supported_single_article"
        elif candidate_decisions:
            status = "world_insufficient"
        else:
            status = "pending_per_document_entailment"
        ledger.append(
            {
                "schema_version": SCHEMA_VERSION,
                "candidate_id": candidate_id,
                "candidate": packet["candidate"],
                "evidence_decisions": candidate_decisions,
                "support_count": counts["supported"],
                "contradiction_count": counts["contradicted"],
                "world_validation_status": status,
                "updated_at": utc_now(),
            }
        )
    write_jsonl(RAG_DIR / "external_evidence_ledger.jsonl", ledger)
    write_jsonl(RAG_DIR / "decision_validation_errors.jsonl", invalid_decisions)
    print(f"Wrote evidence ledger for {len(ledger)} candidates.")
    if invalid_decisions:
        print(f"Rejected {len(invalid_decisions)} invalid decisions; see decision_validation_errors.jsonl")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    build = subparsers.add_parser("build-candidates", help="Create candidate and source-metadata JSONL from extracted tuples.")
    build.add_argument("--tuple-file", default="raw_triplets.txt", help="Per-paper tuple filename to ingest (default: raw_triplets.txt).")
    build.set_defaults(func=command_build_candidates)

    retrieve = subparsers.add_parser("retrieve", help="Retrieve claim-specific external open-access evidence from Europe PMC.")
    retrieve.add_argument("--candidate-id", help="Retrieve evidence for one candidate ID.")
    retrieve.add_argument("--limit", type=int, help="Limit selected candidates; use for a small pilot first.")
    retrieve.add_argument("--articles-per-candidate", type=int, default=5, help="Maximum open-access external articles to retain per candidate.")
    retrieve.add_argument("--top-chunks", type=int, default=3, help="Maximum evidence chunks persisted per external article.")
    retrieve.add_argument("--delay-seconds", type=float, default=0.5, help="Delay between candidate requests.")
    retrieve.set_defaults(func=command_retrieve)

    ingest = subparsers.add_parser("ingest-decisions", help="Create an external evidence ledger from per-document entailment decisions.")
    ingest.add_argument("decisions", help="JSONL decisions with candidate_id, evidence_document_id, decision, and evidence quote/chunk IDs.")
    ingest.set_defaults(func=command_ingest_decisions)
    return parser


if __name__ == "__main__":
    arguments = build_parser().parse_args()
    raise SystemExit(arguments.func(arguments))
