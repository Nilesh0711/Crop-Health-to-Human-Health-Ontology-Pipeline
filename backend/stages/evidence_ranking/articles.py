"""Fuse metadata relevance signals before fetching external text."""

from __future__ import annotations

import re
import math
from collections import Counter
from typing import Any
from backend.stages.evidence_ranking.passages import SemanticReranker
from backend.domain.search_terms import _RELATION_EVIDENCE_TERMS, _retrieval_text, _RETRIEVAL_STOPWORDS


def _anchor_terms(value: object) -> set[str]:
    """Normalize only inflection/initialism variants for metadata coverage."""
    terms: set[str] = set()
    for raw in re.findall(r"[a-z0-9]+", str(value or "").casefold()):
        if raw in _RETRIEVAL_STOPWORDS or len(raw) < 3:
            continue
        if raw.startswith("infect"):
            terms.add("infect")
        elif raw.endswith("s") and len(raw) > 4:
            terms.add(raw[:-1])
        else:
            terms.add(raw)
    return terms


def _condition_initialism(value: object) -> str:
    words = re.findall(r"[A-Za-z]+", str(value or ""))
    return "".join(word[0] for word in words).casefold() if len(words) >= 2 else ""


def metadata_evidence_coverage(candidate: dict[str, Any], preview: str, title: str = "") -> int:
    """Score whether a paper preview names the claim's evidence anchors.

    This is a document-selection signal, not a truth score. A paper can only
    receive a final supported verdict after exact passage-level judgement.
    It prevents broad but semantically related reviews from displacing a study
    whose title/abstract explicitly states the subject, object, and condition.
    """
    text_terms = _anchor_terms(preview)
    title_terms = _anchor_terms(title)
    score = 0
    for field in ("subject", "object"):
        terms = _anchor_terms(candidate.get(field))
        if terms and terms <= text_terms:
            score += 1
    condition = str((candidate.get("qualifiers") or {}).get("condition") or "").strip()
    condition_terms = _anchor_terms(condition)
    initialism = _condition_initialism(condition)
    if condition_terms and (condition_terms <= text_terms or (initialism and initialism in text_terms)):
        score += 1
    relation_terms = _anchor_terms(" ".join(_RELATION_EVIDENCE_TERMS.get(
        str(candidate.get("relation") or ""), ())))
    if relation_terms & text_terms:
        score += 1
    # Search APIs intentionally return broad reviews as well as primary
    # studies. A title that names an exact endpoint/material/condition is a
    # strong *retrieval* signal. It never validates the claim by itself; the
    # passage judge below still has to prove the relation and direction.
    for field, bonus in (("subject", 3), ("object", 2)):
        terms = _anchor_terms(candidate.get(field))
        if terms and terms <= title_terms:
            score += bonus
    if condition_terms and (condition_terms <= title_terms or (initialism and initialism in title_terms)):
        score += 2
    return score


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
    coverage_scores = [metadata_evidence_coverage(candidate, preview, str(item[0].get("title") or ""))
                       for item, preview in zip(eligible, previews)]
    # When similarly relevant studies are available, prefer records that can
    # supply an inspectable full-text passage over title/abstract-only records.
    # This is a retrieval tie-breaker, never evidence by itself.
    fulltext_scores = [1 if identifiers or str(result.get("_open_access_pdf_url") or "").strip() else 0
                       for result, identifiers, _preview in eligible]
    coverage_order = sorted(range(len(eligible)), key=lambda index: coverage_scores[index], reverse=True)
    dense_rank = {index: rank for rank, index in enumerate(dense_order, 1)}
    lexical_rank = {index: rank for rank, index in enumerate(lexical_order, 1)}
    coverage_rank = {index: rank for rank, index in enumerate(coverage_order, 1)}
    fused: list[tuple[float, int]] = []
    for index, item in enumerate(eligible):
        # Standard two-list RRF. Citation provenance is recorded, not injected
        # as a score large enough to dominate lexical and dense relevance.
        # Use exact metadata coverage before cross-encoder truncation. Without
        # this, a paper explicitly naming all claim anchors can be dropped
        # before the cross-encoder ever sees it.
        # Preserve the scholarly API's lexical result order as a third,
        # bounded retrieval signal. In particular, it stops an exact title at
        # OpenAlex rank 1 from being displaced by a generic review merely
        # because the bi-encoder views both as topically similar.
        query_rank = max(1, int(item[0].get("_query_rank") or 10_000))
        api_rank_bonus = (1 / (40 + query_rank)) if coverage_scores[index] >= 3 else 0.0
        fused.append((1 / (60 + dense_rank[index]) + 1 / (60 + lexical_rank[index])
                      + (coverage_scores[index] / 100) + api_rank_bonus
                      + (0.06 * fulltext_scores[index] if coverage_scores[index] >= 3 else 0.0), index))
    fused.sort(reverse=True)
    top = fused[:reranker.settings.cross_encoder_candidates]
    cross_scores = reranker.cross_scores(hypothesis, [previews[index] for _, index in top])
    cross_by_index = {index: score for score, (_, index) in zip(cross_scores, top)}
    rrf_by_index = {index: score for score, index in fused}
    initial_rank = {index: rank for rank, (_score, index) in enumerate(fused, 1)}
    cross_order = sorted((index for _score, index in top), key=lambda index: cross_by_index[index], reverse=True)
    cross_rank = {index: rank for rank, index in enumerate(cross_order, 1)}
    # MedCPT scores can be tightly saturated for broad agronomy abstracts.
    # Fuse its rank with the existing dense+lexical RRF rank so a tiny score
    # difference cannot erase exact endpoint relevance.
    final_by_index = {
        # Coverage is intentionally bounded but decisive when all four anchor
        # groups occur. It fixes candidate selection, not claim acceptance.
        index: (1 / (60 + initial_rank[index]) + 1 / (60 + cross_rank[index])
                + (coverage_scores[index] / 100)
                + (1 / (40 + max(1, int(eligible[index][0].get("_query_rank") or 10_000)))
                   if coverage_scores[index] >= 3 else 0.0)
                + (0.06 * fulltext_scores[index] if coverage_scores[index] >= 3 else 0.0))
        for _score, index in top
    }
    for index, item in enumerate(eligible):
        item[0]["_rrf_score"] = rrf_by_index[index]
        if index in cross_by_index:
            item[0]["_article_cross_encoder_score"] = cross_by_index[index]
        item[0]["_metadata_evidence_coverage"] = coverage_scores[index]
    reranked_top = sorted(top, key=lambda item: final_by_index[item[1]], reverse=True)
    ordered = [*reranked_top, *fused[len(top):]]
    return [(final_by_index.get(index, rrf_score), eligible[index]) for rrf_score, index in ordered]
