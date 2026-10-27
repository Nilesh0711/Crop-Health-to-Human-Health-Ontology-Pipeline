"""Convert extraction files into source-aware validation candidates."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from backend.config.settings import ONTOLOGY_DIR, PDF_DIR, SCHEMA_VERSION
from backend.shared.storage import file_hash, stable_hash, utc_now


EVIDENCE_RE = re.compile(r'^\s*#\s*evidence\s*:\s*["“](.+)["”]\s*$', re.IGNORECASE)


def parse_tuple_line(line: str) -> dict[str, Any] | None:
    """Parse current five-field tuples and legacy six-field tuples."""
    line = line.strip()
    if not line.startswith("<") or not line.endswith(">"):
        return None
    parts = [part.strip() for part in line[1:-1].split(",")]
    if len(parts) not in {5, 6} or not all(parts[:5]):
        return None
    subject, relation, obj, subject_type, object_type = parts[:5]
    parsed = {
        "subject": subject,
        "relation": relation.lower(),
        "object": obj,
        "subject_type": subject_type,
        "object_type": object_type,
    }
    if len(parts) == 6:
        try:
            parsed["legacy_extraction_confidence"] = float(parts[5])
        except ValueError:
            parsed["legacy_extraction_confidence"] = None
    return parsed


def find_source_pdf(paper_name: str) -> Path | None:
    exact_path = PDF_DIR / f"{paper_name}.pdf"
    if exact_path.exists():
        return exact_path
    normalized = paper_name.casefold()
    return next((path for path in PDF_DIR.glob("*.pdf") if path.stem.casefold() == normalized), None)


def build_candidates(tuple_filename: str = "raw_triplets.txt") -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    candidates: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []
    parse_errors: list[dict[str, Any]] = []
    for paper_dir in sorted(path for path in ONTOLOGY_DIR.iterdir() if path.is_dir()):
        tuple_path = paper_dir / tuple_filename
        if not tuple_path.exists():
            continue
        pdf_path = find_source_pdf(paper_dir.name)
        pdf_digest = file_hash(pdf_path) if pdf_path else None
        document_id = "local-" + stable_hash(f"{paper_dir.name}|{pdf_digest or 'missing-pdf'}", 32)
        documents.append({
            "schema_version": SCHEMA_VERSION, "document_id": document_id,
            "document_role": "candidate_origin", "title": paper_dir.name,
            "local_pdf_path": str(pdf_path.relative_to(ONTOLOGY_DIR.parent)) if pdf_path else None,
            "file_sha256": pdf_digest, "source_tuple_file": str(tuple_path.relative_to(ONTOLOGY_DIR.parent)),
            "created_at": utc_now(),
        })
        last_candidate: dict[str, Any] | None = None
        for line_number, raw_line in enumerate(tuple_path.read_text(encoding="utf-8").splitlines(), start=1):
            if not raw_line.strip():
                continue
            evidence_match = EVIDENCE_RE.match(raw_line)
            if evidence_match:
                if last_candidate is None:
                    parse_errors.append({"schema_version": SCHEMA_VERSION, "source_document_id": document_id,
                        "source_tuple_file": str(tuple_path.relative_to(ONTOLOGY_DIR.parent)), "line_number": line_number,
                        "raw_tuple": raw_line, "error": "Evidence comment has no preceding tuple"})
                else:
                    last_candidate["source_evidence_quote"] = evidence_match.group(1)
                continue
            parsed = parse_tuple_line(raw_line)
            if parsed is None:
                parse_errors.append({"schema_version": SCHEMA_VERSION, "source_document_id": document_id,
                    "source_tuple_file": str(tuple_path.relative_to(ONTOLOGY_DIR.parent)), "line_number": line_number,
                    "raw_tuple": raw_line, "error": "Expected a five-field angle-bracket tuple or # evidence comment"})
                last_candidate = None
                continue
            candidate_key = f"{document_id}|{line_number}|{raw_line.strip()}"
            last_candidate = {"schema_version": SCHEMA_VERSION, "candidate_id": "candidate-" + stable_hash(candidate_key, 32),
                "source_document_id": document_id, "source_paper_title": paper_dir.name,
                "source_tuple_file": str(tuple_path.relative_to(ONTOLOGY_DIR.parent)), "source_tuple_line": line_number,
                "raw_tuple": raw_line.strip(), **parsed, "candidate_status": "pending_external_validation",
                "created_at": utc_now()}
            candidates.append(last_candidate)
    return candidates, documents, parse_errors
