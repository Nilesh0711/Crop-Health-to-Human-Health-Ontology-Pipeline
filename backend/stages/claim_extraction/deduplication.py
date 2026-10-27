"""Merge wording variants of the same assertion in the same source sentence."""

from __future__ import annotations

import json
from typing import Any

from backend.domain.alignment import normalized_phrase


def _endpoint(value: object) -> str:
    return " ".join(word for word in normalized_phrase(value).split() if word not in {"a", "an", "the"})


def deduplicate_source_claims(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Never merge different restrictions, source spans, relation direction or types.

    Different certainty labels remain distinct proposals for source assessment;
    deduplication cannot decide which scientific wording is correct.
    """
    unique: list[dict[str, Any]] = []
    seen: dict[tuple[Any, ...], dict[str, Any]] = {}
    duplicates = 0
    for candidate in candidates:
        certainty = str(candidate.get("certainty") or "")
        key = (candidate.get("source_document_id"), candidate.get("source_pdf_sha256"),
               candidate.get("source_page"), normalized_phrase(candidate.get("source_evidence_span")),
               _endpoint(candidate.get("subject")), candidate.get("relation"), _endpoint(candidate.get("object")),
               candidate.get("subject_type"), candidate.get("object_type"), certainty,
               json.dumps(candidate.get("qualifiers") or {}, sort_keys=True),
               normalized_phrase(candidate.get("source_context")))
        # Missing evidence can never establish duplicate identity.
        if not key[3] or key not in seen:
            seen[key] = candidate
            unique.append(candidate)
            continue
        existing = seen[key]
        duplicates += 1
        existing.setdefault("deduplicated_candidate_ids", []).append(candidate["candidate_id"])
        variants = existing.setdefault("extraction_certainty_variants", [existing.get("certainty")])
        if certainty not in variants:
            variants.append(certainty)
        existing.setdefault("source_evidence_records", []).extend(candidate.get("source_evidence_records") or [])
        links = existing.setdefault('source_citation_links', [])
        links.extend(link for link in candidate.get('source_citation_links', []) if link not in links)
        for field in ("source_citation_numbers", "source_citation_keys"):
            existing[field] = sorted(set(existing.get(field, [])) | set(candidate.get(field, [])))
        references = existing.setdefault("source_references", [])
        known = {json.dumps(reference, sort_keys=True) for reference in references}
        for reference in candidate.get("source_references") or []:
            identity = json.dumps(reference, sort_keys=True)
            if identity not in known:
                references.append(reference)
                known.add(identity)
    return unique, duplicates
