"""The one-pass query-translate, HyDE, retrieve, rerank, and judge workflow."""

from __future__ import annotations

import re
from typing import Any, Callable

from .config import SCHEMA_VERSION, Settings
from .decision import judge_document
from .llm import LLMError, OllamaClient
from .rerank import SemanticReranker, generate_hypothesis
from .retrieval import article_metadata, extract_paragraphs, fetch_fulltext_xml, fulltext_identifiers, is_open_access, make_chunks, search
from .storage import stable_hash, utc_now


_QUERY_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}


def translate_query(client: OllamaClient, candidate: dict[str, Any]) -> dict[str, str]:
    """Ask Qwen for a bounded Europe PMC query; never use unverified neighbour claims."""
    system = """You are an academic librarian translating one claim into a concept-only Europe PMC Boolean query. Search for independent research about the exact subject and object. Use 2-5 concise concept terms or short phrases per side, joined with AND; use OR only for genuine synonyms or abbreviations. Never include the originating paper title, authors, journal, DOI, a full claim sentence, or PubMed field suffixes such as [All Fields]. Never invent a mechanism. Return JSON only."""
    user = (f"Target claim: <{candidate['subject']}, {candidate['relation']}, {candidate['object']}>\n"
            f"Entity types: {candidate['subject_type']}, {candidate['object_type']}. "
            f"Qualifiers: {candidate.get('qualifiers') or {}}.\n"
            "Return only {\"query\": \"...\"}. Do not include a rationale or explanation.")
    response = client.chat_json(system, user, max_tokens=160, json_schema=_QUERY_SCHEMA)
    query = str(response.get("query", "")).strip()
    # Europe PMC's REST search accepts plain Boolean syntax reliably. Qwen may
    # suggest field names from other scholarly APIs; remove those rather than
    # silently issuing a zero-result query.
    query = re.sub(r"(?i)\b(?:TITLE_ABS|TITLE|ABSTRACT)\s*:\s*", "", query)
    query = re.sub(r"(?i)\s*\[(?:all fields|title/abstract|mesh terms?)\]", "", query)
    if not query or len(query) > 1500:
        raise LLMError("Qwen did not return a usable Europe PMC query")
    return {"query": query}


def retrieve_round(candidate: dict[str, Any], client: OllamaClient, reranker: SemanticReranker, settings: Settings,
                   articles_per_candidate: int, top_chunks: int,
                   progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    translated = translate_query(client, candidate)
    if progress:
        progress(f"Europe PMC query: {translated['query']}")
    hypothesis = generate_hypothesis(client, candidate)
    # Search a broader metadata-only pool, then let BGE choose which few papers
    # merit transient full-text retrieval.
    results = search(translated["query"], settings, max(articles_per_candidate * 8, 24))
    if progress:
        progress(f"Europe PMC returned {len(results)} search results; selecting open access evidence")
    documents: list[dict[str, Any]] = []
    notes: list[dict[str, str]] = []
    seen: set[str] = set()
    eligible: list[tuple[dict[str, Any], list[tuple[str, str]], str]] = []
    for result in results:
        if not is_open_access(result):
            continue
        source_title = re.sub(r"[^a-z0-9]+", "", str(candidate.get("source_paper_title", "")).casefold())
        result_title = re.sub(r"[^a-z0-9]+", "", re.sub(r"<[^>]+>", "", str(result.get("title", ""))).casefold())
        if source_title and result_title and source_title == result_title:
            notes.append({"article_id": str(result.get("id", "")), "reason": "originating_paper_excluded"})
            continue
        identifiers = fulltext_identifiers(result)
        if not identifiers:
            continue
        article_key = "external-" + stable_hash(f"{result.get('source')}|{result.get('id')}|{result.get('pmcid')}|{result.get('doi')}", 32)
        if article_key in seen:
            continue
        seen.add(article_key)
        preview = " ".join(str(result.get(field, "") or "") for field in ("title", "abstractText", "journalTitle"))
        eligible.append((result, identifiers, preview))

    # Rank search-result titles/abstracts before downloading XML. This prevents
    # the first open-access hits from crowding out more relevant papers while
    # still keeping complete external papers transient and in memory only.
    preview_scores = reranker.rank_texts(hypothesis, [item[2] for item in eligible])
    ranked_results = sorted(zip(preview_scores, eligible), key=lambda item: item[0], reverse=True)
    for article_score, (result, identifiers, _preview) in ranked_results:
        if len(documents) >= articles_per_candidate:
            break
        article_key = "external-" + stable_hash(f"{result.get('source')}|{result.get('id')}|{result.get('pmcid')}|{result.get('doi')}", 32)
        xml_text = next((text for source, article_id in identifiers if (text := fetch_fulltext_xml(source, article_id, settings))), None)
        if not xml_text:
            notes.append({"article_id": article_key, "reason": "full_text_unavailable_or_not_permitted"})
            continue
        chunks = reranker.select(hypothesis, make_chunks(extract_paragraphs(xml_text)), article_key, top_chunks)
        # The full XML now falls out of scope: only selected chunks persist.
        if not chunks:
            notes.append({"article_id": article_key, "reason": "no_paragraphs_for_semantic_reranking"})
            continue
        documents.append({**article_metadata(result, article_key), "article_semantic_score": round(article_score, 6),
                          "evidence_chunks": chunks})
        if progress:
            progress(f"Ranked {len(chunks)} passages: {result.get('title', 'Untitled article')}")
    return {"query_translation": translated, "hyde_hypothesis": hypothesis, "result_count": len(results),
            "open_access_evidence_documents": len(documents), "notes": notes, "evidence_documents": documents}


def validate_candidate(candidate: dict[str, Any], client: OllamaClient, reranker: SemanticReranker, settings: Settings,
                       articles_per_candidate: int = 4, top_chunks: int = 3,
                       progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    if progress:
        progress("External validation")
    result = retrieve_round(candidate, client, reranker, settings, articles_per_candidate, top_chunks, progress)
    decisions = [judge_document(client, candidate, document) for document in result["evidence_documents"]]
    if progress:
        for decision in decisions:
            progress(f"Qwen judged {decision['evidence_document_id']}: {decision['decision']}")
    attempts = [{"attempt": 1, **result, "decisions": decisions}]
    support_ids = {d.get("evidence_document_id") for d in decisions if d.get("decision") == "supported"}
    contradiction_ids = {d.get("evidence_document_id") for d in decisions if d.get("decision") == "contradicted"}
    if support_ids and contradiction_ids:
        status = "world_contested_pending_review"
    elif len(support_ids) >= 2:
        status = "world_corroborated_pending_review"
    elif support_ids:
        status = "world_supported_pending_corroboration"
    elif contradiction_ids:
        status = "world_contradicted_or_contested"
    elif attempts[0]["evidence_documents"]:
        status = "world_insufficient"
    else:
        status = "retrieval_failed_or_no_open_access_evidence"
    return {"schema_version": SCHEMA_VERSION, "candidate_id": candidate["candidate_id"], "candidate": candidate,
            "attempts": attempts, "validation_status": status, "validated_at": utc_now()}


def retrieve_only(candidate: dict[str, Any], client: OllamaClient, reranker: SemanticReranker, settings: Settings,
                  articles_per_candidate: int, top_chunks: int) -> dict[str, Any]:
    result = retrieve_round(candidate, client, reranker, settings, articles_per_candidate, top_chunks)
    return {"schema_version": SCHEMA_VERSION, "candidate_id": candidate["candidate_id"], "candidate": candidate,
            "retrieval": {key: result[key] for key in ("query_translation", "hyde_hypothesis", "result_count", "open_access_evidence_documents", "notes")},
            "evidence_documents": result["evidence_documents"], "validation_status": "pending_per_document_entailment", "retrieved_at": utc_now()}


def evidence_ledger(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ledger: list[dict[str, Any]] = []
    for record in records:
        decisions = [d for attempt in record.get("attempts", []) for d in attempt.get("decisions", [])]
        supports = {d.get("evidence_document_id") for d in decisions if d.get("decision") == "supported"}
        contradictions = {d.get("evidence_document_id") for d in decisions if d.get("decision") == "contradicted"}
        ledger.append({"schema_version": SCHEMA_VERSION, "candidate_id": record["candidate_id"],
                       "candidate": record.get("candidate", {}),
                       "evidence_decisions": decisions, "support_count": len(supports), "contradiction_count": len(contradictions),
                       "world_validation_status": record["validation_status"], "updated_at": utc_now()})
    return ledger
