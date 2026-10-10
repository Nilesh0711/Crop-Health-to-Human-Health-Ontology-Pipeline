"""The one-pass query-translate, HyDE, retrieve, rerank, and judge workflow."""

from __future__ import annotations

import re
import math
from collections import Counter
from typing import Any, Callable

from .config import SCHEMA_VERSION, Settings
from .decision import judge_document
from .llm import LLMError, OllamaClient
from .nli import NLIValidator
from .rerank import SemanticReranker, generate_hypothesis
from .retrieval import (article_metadata, extract_paragraphs, fetch_fulltext_xml,
                        fetch_open_access_pdf_pages, fulltext_identifiers, is_open_access,
                        make_chunks, openalex_open_access_copy, paragraphs_from_pdf_pages,
                        resolve_bibliographic_reference, search, search_openalex)
from .storage import stable_hash, utc_now


_QUERY_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}

_TITLE_WORD_RE = re.compile(r"[a-z0-9]+")
_TITLE_STOPWORDS = {"a", "an", "and", "the", "of", "for", "in", "on", "to", "with", "from", "by", "at", "is", "are"}
_RETRIEVAL_STOPWORDS = _TITLE_STOPWORDS | {"affects", "causes", "reduces", "increases", "leads", "associated", "has"}


def _normal_identifier(value: object) -> str:
    """Normalize DOI-like strings without turning distinct identifiers equal."""
    text = str(value or "").strip().casefold()
    text = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", text)
    text = re.sub(r"^doi\s*:\s*", "", text)
    return text.rstrip(".,;:)")


def _title_terms(value: object) -> set[str]:
    return {word for word in _TITLE_WORD_RE.findall(str(value or "").casefold()) if word not in _TITLE_STOPWORDS}


def originating_source_match(candidate: dict[str, Any], result: dict[str, Any]) -> str | None:
    """Return why a Europe PMC result is the source PDF (or probably is it).

    External corroboration must be independent.  Exact DOI/PMC/PMID identity is
    authoritative.  When PDFs lack identifiers, conservative normalized-title
    containment and high token overlap prevent the same paper returning under a
    title variant.  Potential source matches are excluded rather than allowed
    to create a misleading one-article support state.
    """
    identifiers = (
        ("doi", _normal_identifier(candidate.get("source_doi")), _normal_identifier(result.get("doi"))),
        ("pmcid", str(candidate.get("source_pmcid") or "").strip().casefold(),
         str(result.get("pmcid") or "").strip().casefold()),
        ("pmid", str(candidate.get("source_pmid") or "").strip(), str(result.get("pmid") or "").strip()),
    )
    for name, source_value, result_value in identifiers:
        if source_value and result_value and source_value == result_value:
            return f"originating_paper_excluded_{name}"

    source_title = str(candidate.get("source_paper_title", "")).strip()
    result_title = re.sub(r"<[^>]+>", "", str(result.get("title", ""))).strip()
    source_compact = re.sub(r"[^a-z0-9]+", "", source_title.casefold())
    result_compact = re.sub(r"[^a-z0-9]+", "", result_title.casefold())
    if not source_compact or not result_compact:
        return None
    if source_compact == result_compact:
        return "originating_paper_excluded_title_exact"
    if min(len(source_compact), len(result_compact)) >= 28 and (
            source_compact in result_compact or result_compact in source_compact):
        return "originating_paper_excluded_title_containment"

    source_terms, result_terms = _title_terms(source_title), _title_terms(result_title)
    overlap = len(source_terms & result_terms)
    smaller = min(len(source_terms), len(result_terms))
    # Five matching content words at 90% coverage is a deliberately strict
    # near-duplicate signal. It catches harmless subtitles but not merely
    # related papers about soil health or crop productivity.
    if smaller >= 5 and overlap >= 5 and overlap / smaller >= 0.9:
        return "possible_originating_paper_excluded_title_similarity"
    return None


def translate_query(client: OllamaClient, candidate: dict[str, Any]) -> dict[str, str]:
    """Ask Qwen for a bounded Europe PMC query; never use unverified neighbour claims."""
    system = """You are an academic librarian translating one claim into a concept-only Europe PMC Boolean query. Search for independent research about the exact subject and object. Use 2-5 concise concept terms or short phrases per side, joined with AND; use OR only for genuine synonyms or abbreviations. Never include the originating paper title, authors, journal, DOI, a full claim sentence, or PubMed field suffixes such as [All Fields]. Never invent a mechanism. Return JSON only."""
    user = (f"Target claim: <{candidate['subject']}, {candidate['relation']}, {candidate['object']}>\n"
            f"Entity types: {candidate['subject_type']}, {candidate['object_type']}. "
            f"Qualifiers: {candidate.get('qualifiers') or {}}.\n"
            "Return only {\"query\": \"...\"}. Do not include a rationale or explanation.")
    response = client.chat_json(system, user, max_tokens=160, json_schema=_QUERY_SCHEMA)
    raw_query = str(response.get("query", "")).strip()
    query = raw_query
    # Europe PMC's REST search accepts plain Boolean syntax reliably. Qwen may
    # suggest field names from other scholarly APIs; remove those rather than
    # silently issuing a zero-result query.
    query = re.sub(r"(?i)\b(?:TITLE_ABS|TITLE|ABSTRACT)\s*:\s*", "", query)
    query = re.sub(r"(?i)\s*\[(?:all fields|title/abstract|mesh terms?)\]", "", query)
    # Ontology labels describe our schema; they are not literature concepts.
    # Small models occasionally emit ``SoilBiota AND aeration`` verbatim.
    # Replace only the two labels supplied to the model with their actual
    # endpoint phrases, retaining useful synonyms elsewhere in its query.
    replacements = (
        (candidate.get("subject_type"), candidate.get("subject")),
        (candidate.get("object_type"), candidate.get("object")),
    )
    ontology_label_replaced = False
    for entity_type, entity in replacements:
        label = str(entity_type or "").strip()
        phrase = re.sub(r"[^A-Za-z0-9 -]+", " ", str(entity or "")).strip()
        if not label or not phrase:
            continue
        spaced_label = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", label)
        repaired = re.sub(rf"(?i)\b(?:{re.escape(label)}|{re.escape(spaced_label)})\b", phrase, query)
        ontology_label_replaced = ontology_label_replaced or repaired != query
        query = repaired
    query = " ".join(query.split())
    if not query or len(query) > 1500:
        raise LLMError("Qwen did not return a usable Europe PMC query")
    strategy = "ontology_labels_replaced" if ontology_label_replaced else "sanitized" if query != raw_query else "qwen"
    return {"query": query, "raw_query": raw_query, "query_strategy": strategy}


def _result_key(result: dict[str, Any]) -> str:
    doi = _normal_identifier(result.get("doi"))
    if doi:
        return f"doi:{doi}"
    pmcid = str(result.get("pmcid") or "").strip().casefold()
    if pmcid:
        return f"pmcid:{pmcid}"
    return "source:" + "|".join(str(result.get(name) or "").casefold() for name in ("source", "id"))


def _citation_results(candidate: dict[str, Any], settings: Settings,
                      notes: list[dict[str, str]]) -> list[dict[str, Any]]:
    """Resolve and search references attached to the exact source span first."""
    results: list[dict[str, Any]] = []
    for reference in (candidate.get("source_references") or [])[:6]:
        resolved = resolve_bibliographic_reference(reference, settings)
        if not resolved:
            notes.append({"article_id": f"reference-{reference.get('reference_number', '?')}",
                          "reason": "source_citation_could_not_be_resolved"})
            continue
        if resolved.get("doi"):
            query = f'DOI:"{resolved["doi"]}"'
        elif resolved.get("title"):
            query = f'"{resolved["title"]}"'
        else:
            continue
        try:
            matches = search(query, settings, 5)
        except (OSError, ValueError):
            matches = []
        openalex = openalex_open_access_copy(str(resolved.get("doi") or ""), settings)
        if not matches:
            notes.append({"article_id": f"reference-{reference.get('reference_number', '?')}",
                          "reason": "resolved_source_citation_not_indexed_in_europe_pmc"})
            # For a cited DOI Europe PMC did not index, ask OpenAlex for that
            # exact work's indexed abstract or explicit public PDF rather than
            # using unbounded web retrieval.
            if openalex:
                openalex["_retrieval_route"] = "source_citation_openalex"
                openalex["_source_reference_number"] = reference.get("reference_number")
                results.append(openalex)
            else:
                notes.append({"article_id": f"reference-{reference.get('reference_number', '?')}",
                              "reason": "cited_doi_has_no_openalex_abstract_or_public_pdf"})
        for result in matches:
            enriched = dict(result)
            enriched["_retrieval_route"] = "source_citation"
            enriched["_source_reference_number"] = reference.get("reference_number")
            # Europe PMC may resolve citation metadata while exposing neither
            # abstract nor usable full text. Retain OpenAlex's abstract or
            # explicit OA PDF as a fallback on that same bibliographic record.
            if openalex:
                enriched["_open_access_pdf_url"] = openalex.get("_open_access_pdf_url")
                enriched["_landing_page_url"] = openalex.get("_landing_page_url")
                enriched["_open_access_license"] = openalex.get("_open_access_license")
                if not enriched.get("abstractText"):
                    enriched["abstractText"] = openalex.get("abstractText")
            results.append(enriched)
    return results


def _retrieval_text(candidate: dict[str, Any]) -> str:
    qualifiers = " ".join(str(value) for value in (candidate.get("qualifiers") or {}).values() if value)
    return f"{candidate.get('subject', '')} {candidate.get('object', '')} {qualifiers}".strip()


def _bm25_scores(query: str, documents: list[str]) -> list[float]:
    """Small dependency-free BM25 scorer for metadata-level hybrid retrieval."""
    tokenized = [[term for term in re.findall(r"[a-z0-9]+", text.casefold()) if term not in _RETRIEVAL_STOPWORDS]
                 for text in documents]
    query_terms = [term for term in re.findall(r"[a-z0-9]+", query.casefold()) if term not in _RETRIEVAL_STOPWORDS]
    if not tokenized or not query_terms:
        return [0.0] * len(documents)
    document_frequency = Counter(term for terms in tokenized for term in set(terms))
    average_length = sum(map(len, tokenized)) / max(1, len(tokenized))
    scores: list[float] = []
    for terms in tokenized:
        counts = Counter(terms)
        score = 0.0
        for term in set(query_terms):
            frequency = counts[term]
            if not frequency:
                continue
            inverse_frequency = math.log(1 + (len(tokenized) - document_frequency[term] + 0.5)
                                         / (document_frequency[term] + 0.5))
            denominator = frequency + 1.2 * (1 - 0.75 + 0.75 * len(terms) / max(1.0, average_length))
            score += inverse_frequency * frequency * 2.2 / denominator
        scores.append(score)
    return scores


def _hybrid_order(hypothesis: str, candidate: dict[str, Any], eligible: list[tuple[dict[str, Any], list[tuple[str, str]], str]],
                  reranker: SemanticReranker) -> list[tuple[float, tuple[dict[str, Any], list[tuple[str, str]], str]]]:
    previews = [item[2] for item in eligible]
    dense_scores = reranker.rank_texts(hypothesis, previews)
    lexical_scores = _bm25_scores(_retrieval_text(candidate), previews)
    dense_order = sorted(range(len(eligible)), key=lambda index: dense_scores[index], reverse=True)
    lexical_order = sorted(range(len(eligible)), key=lambda index: lexical_scores[index], reverse=True)
    dense_rank = {index: rank for rank, index in enumerate(dense_order, 1)}
    lexical_rank = {index: rank for rank, index in enumerate(lexical_order, 1)}
    fused: list[tuple[float, int]] = []
    for index, item in enumerate(eligible):
        # Standard two-list RRF. Citation provenance is recorded, not injected
        # as a score large enough to dominate lexical and dense relevance.
        fused.append((1 / (60 + dense_rank[index]) + 1 / (60 + lexical_rank[index]), index))
    fused.sort(reverse=True)
    top = fused[:reranker.settings.cross_encoder_candidates]
    cross_scores = reranker.cross_scores(hypothesis, [previews[index] for _, index in top])
    cross_by_index = {index: score for score, (_, index) in zip(cross_scores, top)}
    rrf_by_index = {index: score for score, index in fused}
    for index, item in enumerate(eligible):
        item[0]["_rrf_score"] = rrf_by_index[index]
        if index in cross_by_index:
            item[0]["_article_cross_encoder_score"] = cross_by_index[index]
    reranked_top = sorted(top, key=lambda item: cross_by_index[item[1]], reverse=True)
    ordered = [*reranked_top, *fused[len(top):]]
    return [(cross_by_index.get(index, rrf_score), eligible[index]) for rrf_score, index in ordered]


def _evidence_level(route: str) -> str:
    return "source_reference" if route.startswith("source_citation") else "independent_external"


def _independence_group(document: dict[str, Any]) -> str:
    """Conservative publication/study-family key for corroboration counts."""
    doi = _normal_identifier(document.get("doi"))
    if doi:
        return f"doi:{doi}"
    pmid = str(document.get("pmid") or "").strip()
    if pmid:
        return f"pmid:{pmid}"
    first_author = str(document.get("author_string") or "").split(",", 1)[0].casefold().strip()
    year = str(document.get("publication_year") or "").strip()
    title = "-".join(sorted(_title_terms(document.get("title"))))
    return f"fallback:{first_author}|{year}|{title}"


def retrieve_round(candidate: dict[str, Any], client: OllamaClient, reranker: SemanticReranker, settings: Settings,
                   articles_per_candidate: int, top_chunks: int,
                   progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    translated = translate_query(client, candidate)
    if progress:
        progress(f"Europe PMC query: {translated['query']}")
    hypothesis = generate_hypothesis(client, candidate)
    # Search a broader metadata-only pool, then let BGE choose which few papers
    # merit transient full-text retrieval.
    notes: list[dict[str, str]] = []
    cited_results = _citation_results(candidate, settings, notes)
    try:
        broad_results = search(translated["query"], settings, max(articles_per_candidate * 25, 100))
    except (OSError, ValueError):
        broad_results = []
        notes.append({"article_id": "europe-pmc-broad-search", "reason": "europe_pmc_search_unavailable"})
    try:
        openalex_results = search_openalex(_retrieval_text(candidate), settings,
                                           max(articles_per_candidate * 25, 100))
    except (OSError, ValueError):
        openalex_results = []
        notes.append({"article_id": "openalex-broad-search", "reason": "openalex_search_unavailable"})
    results: list[dict[str, Any]] = []
    seen_results: dict[str, dict[str, Any]] = {}
    for result in [*cited_results, *broad_results, *openalex_results]:
        key = _result_key(result)
        existing = seen_results.get(key)
        if existing is not None:
            # Preserve the higher-priority route while borrowing an explicit
            # OA PDF/landing page or abstract from the duplicate index record.
            for field in ("_open_access_pdf_url", "_landing_page_url", "_open_access_license", "abstractText"):
                if not existing.get(field) and result.get(field):
                    existing[field] = result[field]
            if is_open_access(result):
                existing["isOpenAccess"] = "Y"
            continue
        enriched = dict(result)
        seen_results[key] = enriched
        results.append(enriched)
    if progress:
        progress(f"Literature search returned {len(broad_results)} Europe PMC results, "
                 f"{len(openalex_results)} OpenAlex results, and {len(cited_results)} cited-paper matches; "
                 "selecting full-text or abstract evidence")
    documents: list[dict[str, Any]] = []
    seen: set[str] = set()
    eligible: list[tuple[dict[str, Any], list[tuple[str, str]], str]] = []
    for result in results:
        same_source_reason = originating_source_match(candidate, result)
        if same_source_reason:
            notes.append({"article_id": str(result.get("id", "")), "reason": same_source_reason})
            continue
        identifiers = fulltext_identifiers(result) if is_open_access(result) else []
        direct_pdf_url = str(result.get("_open_access_pdf_url") or "").strip()
        abstract = str(result.get("abstractText") or "").strip()
        if not identifiers and not direct_pdf_url and len(abstract) < 80:
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
    ranked_results = _hybrid_order(hypothesis, candidate, eligible, reranker)
    reference_documents_selected = 0
    reference_limit = 1 if articles_per_candidate > 1 else articles_per_candidate
    for article_score, (result, identifiers, _preview) in ranked_results:
        if len(documents) >= articles_per_candidate:
            break
        route = str(result.get("_retrieval_route", "broad_search"))
        if route.startswith("source_citation") and reference_documents_selected >= reference_limit:
            continue
        article_key = "external-" + stable_hash(f"{result.get('source')}|{result.get('id')}|{result.get('pmcid')}|{result.get('doi')}", 32)
        direct_pdf_url = str(result.get("_open_access_pdf_url") or "").strip()
        # Prefer structured Europe PMC XML. If it is unavailable or unusable,
        # fall back to an explicit OpenAlex open-access PDF for the same work.
        # The PDF remains in memory and only selected, page-labelled passages
        # are persisted.
        xml_text = next(
            (text for source, article_id in identifiers if (text := fetch_fulltext_xml(source, article_id, settings))), None)
        direct_pdf_pages = (
            fetch_open_access_pdf_pages(direct_pdf_url, settings)
            if direct_pdf_url and not xml_text else []
        )
        paragraphs = (paragraphs_from_pdf_pages(direct_pdf_pages) if direct_pdf_pages
                      else extract_paragraphs(xml_text) if xml_text else [])
        evidence_access = "full_text"
        if not paragraphs:
            abstract = str(result.get("abstractText") or "").strip()
            if len(abstract) < 80:
                notes.append({"article_id": article_key, "reason": "full_text_and_abstract_unavailable"})
                continue
            paragraphs = [abstract]
            evidence_access = "abstract"
        chunks = reranker.select(hypothesis, make_chunks(paragraphs), article_key, top_chunks)
        # The full XML now falls out of scope: only selected chunks persist.
        if not chunks:
            notes.append({"article_id": article_key, "reason": "no_paragraphs_for_semantic_reranking"})
            continue
        document = {**article_metadata(result, article_key), "article_semantic_score": round(article_score, 6),
                          "rrf_score": round(float(result.get("_rrf_score") or 0.0), 6),
                          "article_cross_encoder_score": round(float(result.get("_article_cross_encoder_score") or 0.0), 6),
                          "hybrid_retrieval_score": round(article_score, 6),
                          "retrieval_route": route,
                          "evidence_level": _evidence_level(route),
                          "source_reference_number": result.get("_source_reference_number"),
                          "evidence_access": evidence_access,
                          "evidence_chunks": chunks}
        document["independence_group"] = _independence_group(document)
        documents.append(document)
        if route.startswith("source_citation"):
            reference_documents_selected += 1
        if progress:
            progress(f"Ranked {len(chunks)} passages: {result.get('title', 'Untitled article')}")
    return {"query_translation": translated, "hyde_hypothesis": hypothesis,
            "result_count": len(broad_results) + len(openalex_results),
            "europe_pmc_result_count": len(broad_results),
            "openalex_result_count": len(openalex_results),
            "cited_result_count": len(cited_results),
            "evidence_documents_count": len(documents),
            "open_access_evidence_documents": sum(document.get("evidence_access") == "full_text" for document in documents),
            "notes": notes, "evidence_documents": documents}


def validate_candidate(candidate: dict[str, Any], client: OllamaClient, reranker: SemanticReranker, settings: Settings,
                       articles_per_candidate: int = 4, top_chunks: int = 3,
                       progress: Callable[[str], None] | None = None,
                       nli_validator: NLIValidator | None = None) -> dict[str, Any]:
    if progress:
        progress("External validation")
    result = retrieve_round(candidate, client, reranker, settings, articles_per_candidate, top_chunks, progress)
    decisions = [judge_document(client, candidate, document, nli_validator=nli_validator)
                 for document in result["evidence_documents"]]
    if progress:
        for decision in decisions:
            progress(f"Qwen judged {decision['evidence_document_id']}: {decision['decision']}")
    attempts = [{"attempt": 1, **result, "decisions": decisions}]
    document_by_id = {document["evidence_document_id"]: document for document in result["evidence_documents"]}
    support_ids = {d.get("evidence_document_id") for d in decisions if d.get("decision") == "supported"}
    reference_support_ids = {identifier for identifier in support_ids
                             if document_by_id.get(identifier, {}).get("evidence_level") == "source_reference"}
    independent_support_ids = {identifier for identifier in support_ids
                               if document_by_id.get(identifier, {}).get("evidence_level") == "independent_external"}
    independent_groups = {document_by_id[identifier].get("independence_group") for identifier in independent_support_ids}
    contradiction_ids = {d.get("evidence_document_id") for d in decisions if d.get("decision") == "contradicted"}
    if support_ids and contradiction_ids:
        status = "world_contested_pending_review"
    elif len(independent_groups) >= 2:
        status = "world_independently_corroborated_pending_review"
    elif independent_support_ids:
        status = "world_independent_supported_pending_corroboration"
    elif reference_support_ids:
        status = "source_reference_supported_pending_independent_corroboration"
    elif contradiction_ids:
        status = "world_contradicted_or_contested"
    elif attempts[0]["evidence_documents"]:
        status = "external_evidence_not_found"
    else:
        status = "retrieval_failed_or_no_open_access_evidence"
    return {"schema_version": SCHEMA_VERSION, "candidate_id": candidate["candidate_id"], "candidate": candidate,
            "attempts": attempts, "validation_status": status,
            "support_summary": {"source_reference": len(reference_support_ids),
                                "independent_external": len(independent_support_ids),
                                "independent_groups": len(independent_groups)},
            "validated_at": utc_now()}


def retrieve_only(candidate: dict[str, Any], client: OllamaClient, reranker: SemanticReranker, settings: Settings,
                  articles_per_candidate: int, top_chunks: int) -> dict[str, Any]:
    result = retrieve_round(candidate, client, reranker, settings, articles_per_candidate, top_chunks)
    return {"schema_version": SCHEMA_VERSION, "candidate_id": candidate["candidate_id"], "candidate": candidate,
            "retrieval": {key: result[key] for key in ("query_translation", "hyde_hypothesis", "result_count", "europe_pmc_result_count", "openalex_result_count", "cited_result_count", "evidence_documents_count", "open_access_evidence_documents", "notes")},
            "evidence_documents": result["evidence_documents"], "validation_status": "pending_per_document_entailment", "retrieved_at": utc_now()}


def evidence_ledger(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ledger: list[dict[str, Any]] = []
    for record in records:
        decisions = [d for attempt in record.get("attempts", []) for d in attempt.get("decisions", [])]
        supports = {d.get("evidence_document_id") for d in decisions if d.get("decision") == "supported"}
        reference_supports = {d.get("evidence_document_id") for d in decisions
                              if d.get("decision") == "supported" and d.get("evidence_level") == "source_reference"}
        independent_supports = supports - reference_supports
        contradictions = {d.get("evidence_document_id") for d in decisions if d.get("decision") == "contradicted"}
        ledger.append({"schema_version": SCHEMA_VERSION, "candidate_id": record["candidate_id"],
                       "candidate": record.get("candidate", {}),
                       "evidence_decisions": decisions, "support_count": len(supports),
                       "source_reference_support_count": len(reference_supports),
                       "independent_support_count": len(independent_supports),
                       "contradiction_count": len(contradictions),
                       "world_validation_status": record["validation_status"], "updated_at": utc_now()})
    return ledger
