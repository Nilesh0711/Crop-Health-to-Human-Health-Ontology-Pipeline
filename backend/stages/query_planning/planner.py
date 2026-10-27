"""Plan complementary literature queries from fixed claims and local source context."""

from __future__ import annotations

import re
from typing import Any, Callable
from backend.domain.search_terms import _RELATION_EVIDENCE_TERMS, _retrieval_endpoint, _retrieval_text
from backend.shared.batching import iter_batched_judgments
from backend.providers.llm import LLMError, OpenCodeClient


_QUERY_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string", "maxLength": 1000},
                   "openalex_query": {"type": "string", "maxLength": 240}},
    "required": ["query", "openalex_query"],
    "additionalProperties": False,
}


def _check_query_response_complete(response):
    from backend.providers.structured_output import IncompleteResponseError
    if any(not str(response.get(key) or '').strip() for key in ('query', 'openalex_query')):
        raise IncompleteResponseError('Query response omitted a required literature-search query')


def translate_query(client: OpenCodeClient, candidate: dict[str, Any]) -> dict[str, str]:
    """Plan bounded literature queries without unverified neighbouring claims."""
    system = """You are an academic librarian. Translate one claim into a Europe PMC Boolean query (query) and a concise OpenAlex plain-text query (openalex_query). Search the exact subject, measured outcome, and essential restrictions. Use 2-5 terms per side; OR is only for synonyms or source-defined abbreviations. Expand abbreviations from supplied source context only. OpenAlex query should use 3-10 discriminating scientific words, no Boolean syntax. Never include source-paper bibliographic metadata, ontology labels, a full sentence, or PubMed field suffixes. Never invent a mechanism or replace an outcome by a related process. Return JSON only."""
    user = (f"Target claim: <{candidate['subject']}, {candidate['relation']}, {candidate['object']}>\n"
            f"Entity types: {candidate['subject_type']}, {candidate['object_type']}. "
            f"Qualifiers: {candidate.get('qualifiers') or {}}.\n"
            f"Occurrence provenance (not a required study restriction): {candidate.get('source_context', '')}\n"
            f"Source evidence: {candidate.get('source_evidence_span', '')}\n"
            f"Bounded local source context (resolve references, not new facts): {candidate.get('source_local_context', '')}\n"
            f"Contextual restatement (not external evidence): {candidate.get('claim_text', '')}\n"
            "Return only {\"query\": \"...\", \"openalex_query\": \"...\"}. No explanation.")
    system += (" For descriptive crop-contaminant occurrence queries, geography, sampling site and farming system "
               "stored as occurrence provenance must not become mandatory search terms. Search the crop and contaminant; "
               "retain genuinely essential biological restrictions in qualifiers. Do not generalize actual treatment or disease conditions.")
    response = client.chat_json(system, user, max_tokens=160, json_schema=_QUERY_SCHEMA,
                                request_label=f"query: {candidate.get('candidate_id', 'claim')}",
                                response_check=_check_query_response_complete)
    raw_query = str(response.get("query", "")).strip()
    query = raw_query
    # Europe PMC's REST search accepts plain Boolean syntax reliably. The query model may
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
    disease_phrases: list[str] = []
    for role in ("subject", "object"):
        if candidate.get(f"{role}_type") == "PlantDisease":
            disease_phrases.append(str(candidate.get(role) or "").strip())
    condition = str((candidate.get("qualifiers") or {}).get("condition") or "").strip()
    if condition and re.search(r"(?i)\b(?:disease|blight|smut|rust|spot|mildew|rot|wilt|scab)\b", condition):
        disease_phrases.append(condition)
    disease_phrase_quoted = False
    for phrase in disease_phrases:
        if len(phrase.split()) < 2 or f'"{phrase.casefold()}"' in query.casefold():
            continue
        repaired = re.sub(rf"(?i)(?<![\w\"]){re.escape(phrase)}(?![\w\"])", f'"{phrase}"', query)
        disease_phrase_quoted = disease_phrase_quoted or repaired != query
        query = repaired
    disease_hint_added = False
    if ("PlantDisease" in {candidate.get("subject_type"), candidate.get("object_type")}
            and not re.search(r"(?i)\b(?:disease|infection|pathogen)\b", query)):
        query = f"({query}) AND (disease OR infection)"
        disease_hint_added = True
    if not query or len(query) > 1500:
        raise LLMError("Query planner did not return a usable Europe PMC query")
    strategies = []
    if ontology_label_replaced:
        strategies.append("ontology_labels_replaced")
    elif query != raw_query and not disease_hint_added:
        strategies.append("sanitized")
    if disease_hint_added:
        strategies.append("disease_type_hint_added")
    if disease_phrase_quoted:
        strategies.append("disease_phrase_quoted")
    strategy = "+".join(strategies) or "helper_llm"
    openalex_query = str(response.get("openalex_query") or "").strip()
    if len(openalex_query) > 240 or re.search(r"\b(?:AND|OR|NOT)\b|\[|\]", openalex_query):
        openalex_query = ""
    return {"query": query, "raw_query": raw_query, "query_strategy": strategy,
            "openalex_query": openalex_query}


def translate_queries(candidates: list[dict[str, Any]], client: Any, batch_size: int = 6,
                      progress: Callable[[str], None] | None = None,
                      on_checkpoint: Callable[[dict[str, Any], dict[str, Any]], None] | None = None) -> dict[str, dict[str, Any]]:
    results = {}
    for candidate, result in iter_batched_judgments(
            candidates, client, lambda candidate, judge: translate_query(judge, candidate), batch_size,
            progress=progress, label="Literature query"):
        results[candidate['candidate_id']] = result
        if on_checkpoint:
            on_checkpoint(candidate, result)
    return results


def evidence_query_plan(candidate: dict[str, Any], translated: dict[str, str]) -> dict[str, list[str]]:
    """Plan complementary discovery queries in one retrieval pass.

    Scientific-verification work separates document discovery, sentence
    rationale selection, and entailment.  A literal claim query alone misses
    papers whose abstracts report an outcome as a measurement (for example,
    disease *incidence* or *severity* instead of "associated with").  This
    planner retains that literal query and adds one bounded measurement query;
    it is not an LLM retry and it cannot change the final claim.
    """
    primary = str(translated.get("query") or "").strip()
    subject = _retrieval_endpoint(candidate.get("subject"))
    obj = _retrieval_endpoint(candidate.get("object"))
    terms = _RELATION_EVIDENCE_TERMS.get(str(candidate.get("relation") or ""), ())
    condition = _retrieval_endpoint((candidate.get("qualifiers") or {}).get("condition"))
    europe_queries = [primary] if primary else []
    # OpenAlex relevance search is plain text, not Boolean. A compact literal
    # query keeps exact-title studies discoverable; an overlong all-terms query
    # can unexpectedly filter them out before reranking.
    openalex_literal = f"{subject} {obj} {condition}".strip() or _retrieval_text(candidate)
    planned_openalex = str(translated.get("openalex_query") or "").strip()
    openalex_queries = [planned_openalex or openalex_literal]
    if subject and obj and terms:
        measurements = " OR ".join(terms)
        condition_clause = f' AND "{condition}"' if condition else ""
        # Quote both plural and ordinary singular forms. This is a lexical
        # query expansion for any endpoint, not a curated toxin-name table.
        singular = re.sub(r"(?i)s$", "", obj) if len(obj) > 4 and obj.casefold().endswith("s") else obj
        object_clause = f'("{obj}" OR "{singular}")' if singular != obj else f'"{obj}"'
        precision = f'("{subject}" AND {object_clause}){condition_clause} AND ({measurements})'
        if precision.casefold() not in {query.casefold() for query in europe_queries}:
            europe_queries.append(precision)
        openalex_precision = (openalex_literal if planned_openalex
                             else f"{subject} {singular} {condition} {' '.join(terms)}")
        if openalex_precision.casefold() not in {query.casefold() for query in openalex_queries}:
            openalex_queries.append(openalex_precision)
    # A direction-neutral endpoint query can discover null/opposite findings
    # and papers missed by the translated mechanism query.
    if subject and obj:
        neutral = f'"{subject}" AND "{obj}"'
        if neutral.casefold() not in {q.casefold() for q in europe_queries}:
            europe_queries.append(neutral)
        if openalex_literal.casefold() not in {q.casefold() for q in openalex_queries}:
            openalex_queries.append(openalex_literal)
    return {"europe_pmc": europe_queries[:3], "openalex": openalex_queries[:3]}


