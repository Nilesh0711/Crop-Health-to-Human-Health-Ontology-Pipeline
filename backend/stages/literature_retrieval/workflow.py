"""Resolve citations, discover and fetch papers, and retain ranked evidence with coverage."""

from __future__ import annotations

import json
import re
from backend.shared.cancellation import check_cancelled, submit_work
from concurrent.futures import ThreadPoolExecutor, as_completed
import time
from typing import Any, Callable
from backend.config.settings import SCHEMA_VERSION, Settings
from backend.shared.cache import RunCache
from backend.shared.activity import TaggedMessage
from backend.providers.llm import OpenCodeClient
from backend.stages.evidence_ranking.passages import SemanticReranker
from backend.domain.claims import generate_hypothesis
from backend.stages.literature_retrieval.clients import (
    article_metadata,
    extract_paragraphs,
    fetch_fulltext_xml,
    fetch_open_access_pdf_pages,
    fulltext_identifiers,
    is_open_access,
    make_chunks,
    openalex_open_access_copy,
    paragraphs_from_pdf_pages,
    resolve_bibliographic_reference,
    search,
    search_openalex,
    search_pubmed,
    unpaywall_open_access_copy,
    europe_pmc_open_access_pdf,
    scholarly_sentences,
)
from backend.shared.storage import stable_hash, utc_now
from backend.domain.search_terms import _TITLE_STOPWORDS
from backend.stages.evidence_ranking.articles import _hybrid_order
from backend.stages.query_planning.planner import evidence_query_plan, translate_query
from backend.stages.literature_retrieval.scheduling import collect_searches


_TITLE_WORD_RE = re.compile(r"[a-z0-9]+")


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


def _result_key(result: dict[str, Any]) -> str:
    doi = _normal_identifier(result.get("doi"))
    if doi:
        return f"doi:{doi}"
    pmcid = str(result.get("pmcid") or "").strip().casefold()
    if pmcid:
        return f"pmcid:{pmcid}"
    return "source:" + "|".join(str(result.get(name) or "").casefold() for name in ("source", "id"))


def _bibliographic_key(result: dict[str, Any]) -> str | None:
    """Conservatively group matching publications even with conflicting index DOIs.

    An identical substantial title, first-author string and year may not create
    two independent evidence votes. No topic/chemical aliases are involved.
    """
    title = ' '.join(re.findall(r'\w+', str(result.get('title') or '').casefold()))
    author = str(result.get('authorString') or result.get('author_string') or '').split(',', 1)[0]
    author = ' '.join(re.findall(r'\w+', author.casefold()))
    year = str(result.get('pubYear') or result.get('publication_year') or '').strip()
    return f'bibliography:{title}|{author}|{year}' if len(title.split()) >= 6 and author and year else None


def _citation_results(candidate: dict[str, Any], settings: Settings,
                      notes: list[dict[str, str]], cache: RunCache | None = None,
                      ledger: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    """Resolve and search references attached to the exact source span first."""
    results: list[dict[str, Any]] = []
    cache = cache if cache is not None else RunCache()
    for reference in (candidate.get("source_references") or []):
        identity = json.dumps({key: value for key, value in reference.items()
                               if key not in {"reference_number", "citation_key"}}, sort_keys=True)
        def resolve():
            details = {}
            result = resolve_bibliographic_reference(reference,settings,diagnostics=details)
            return {'reference':result,'diagnostics':details}
        resolution = cache.get_or_compute("citation-resolution-v2:" + stable_hash(identity,32),resolve)
        resolved = resolution['reference']
        if ledger is not None:
            ledger['resolution_diagnostics'] = resolution['diagnostics']
        if not resolved:
            if ledger is not None:
                ledger.update(status='unavailable', reason=resolution['diagnostics'].get('reason','bibliographic_resolution_failed'))
            notes.append({"article_id": f"reference-{reference.get('reference_number', '?')}",
                          "reason": "source_citation_could_not_be_resolved"})
            continue
        if ledger is not None:
            ledger.update(resolved=True, status='resolved', doi=resolved.get('doi'),
                          resolution_method=resolved.get('resolution_method'))
        if resolved.get("doi"):
            query = f'DOI:"{resolved["doi"]}"'
        elif resolved.get("title"):
            query = f'"{resolved["title"]}"'
        else:
            continue
        try:
            matches = cache.get_or_compute("citation-search:" + query, lambda: search(query, settings, 5))
        except (OSError, ValueError):
            matches = []
        doi = str(resolved.get("doi") or "")
        openalex = cache.get_or_compute("citation-openalex:" + doi,
                                       lambda: openalex_open_access_copy(doi, settings))
        unpaywall = (cache.get_or_compute("citation-unpaywall:" + doi,
                                          lambda: unpaywall_open_access_copy(doi, settings))
                     if doi else None)
        if unpaywall and unpaywall.get('_open_access_pdf_url'):
            if openalex and not openalex.get('_open_access_pdf_url'):
                openalex['_open_access_pdf_url'] = unpaywall['_open_access_pdf_url']
                openalex['_open_access_pdf_urls'] = unpaywall.get('_open_access_pdf_urls', [])
        if doi:
            valid_matches = []
            for result in matches:
                if _normal_identifier(result.get('doi')) == _normal_identifier(doi):
                    valid_matches.append(result)
                else:
                    notes.append({'article_id': _result_key(result), 'reason': 'citation_lookup_doi_mismatch'})
            matches = valid_matches
            if openalex and _normal_identifier(openalex.get('doi')) != _normal_identifier(doi):
                notes.append({'article_id': _result_key(openalex), 'reason': 'citation_lookup_doi_mismatch'})
                openalex = None
        if not matches:
            notes.append({"article_id": f"reference-{reference.get('reference_number', '?')}",
                          "reason": "resolved_source_citation_not_indexed_in_europe_pmc"})
            if openalex:
                openalex["_retrieval_route"] = "source_citation_openalex"
                openalex["_source_reference_number"] = reference.get("reference_number")
                openalex['_source_reference_text'] = reference.get('citation_text')
                openalex['_resolution_method'] = resolved.get('resolution_method')
                results.append(openalex)
            elif unpaywall:
                unpaywall["_retrieval_route"] = "source_citation_unpaywall"
                unpaywall["_source_reference_number"] = reference.get("reference_number")
                unpaywall['_source_reference_text'] = reference.get('citation_text')
                unpaywall['_resolution_method'] = resolved.get('resolution_method')
                results.append(unpaywall)
            else:
                notes.append({"article_id": f"reference-{reference.get('reference_number', '?')}",
                              "reason": "cited_doi_has_no_accessible_oa_pdf_or_abstract"})
        for result in matches:
            enriched = dict(result)
            enriched["_retrieval_route"] = "source_citation"
            enriched["_source_reference_number"] = reference.get("reference_number")
            enriched['_source_reference_text'] = reference.get('citation_text')
            enriched['_resolution_method'] = resolved.get('resolution_method')
            # Europe PMC may resolve citation metadata while exposing neither
            # abstract nor usable full text. Retain OpenAlex's abstract or
            # explicit OA PDF as a fallback on that same bibliographic record.
            if openalex:
                enriched["_open_access_pdf_url"] = enriched.get("_open_access_pdf_url") or openalex.get("_open_access_pdf_url")
                enriched["_landing_page_url"] = enriched.get("_landing_page_url") or openalex.get("_landing_page_url")
                enriched["_open_access_license"] = enriched.get("_open_access_license") or openalex.get("_open_access_license")
                if not enriched.get("abstractText"):
                    enriched["abstractText"] = openalex.get("abstractText")
            if unpaywall:
                enriched["_open_access_pdf_url"] = enriched.get("_open_access_pdf_url") or unpaywall.get("_open_access_pdf_url")
                enriched["_landing_page_url"] = enriched.get("_landing_page_url") or unpaywall.get("_landing_page_url")
            if not enriched.get("abstractText") and doi:
                try:
                    pm_results = cache.get_or_compute("citation-pm:" + doi, lambda: search_pubmed(doi, settings, 1))
                    if pm_results and pm_results[0].get("abstractText"):
                        enriched["abstractText"] = pm_results[0]["abstractText"]
                except Exception:
                    pass
            results.append(enriched)
        if ledger is not None:
            for result in results:
                result.setdefault('_source_reference_ids', []).append(ledger['reference_id'])
            ledger.update(status='resolved' if results else 'unavailable',
                          reason='' if results else 'resolved_but_no_indexed_accessible_copy')
    return results


def citation_tracking(candidate):
    """One row per attached reference, plus citation markers lacking a link."""
    rows = []
    for reference in candidate.get('source_references') or []:
        identifier = stable_hash(json.dumps(reference, sort_keys=True), 24)
        rows.append({'reference_id': identifier, 'citation_key': str(reference.get('citation_key') or reference.get('reference_number')),
                     'chapter_id': reference.get('chapter_id'), 'citation_text': reference.get('citation_text'),
                     'identified': True, 'resolved': False, 'fetched': False, 'included_in_judgement': False,
                     'status': 'identified', 'reason': '', 'evidence_document_ids': []})
        if candidate.get('citation_origins',{}).get(identifier):
            rows[-1]['source_claims'] = candidate['citation_origins'][identifier]
    for link in candidate.get('source_citation_links') or []:
        if link.get('status') == 'missing_bibliography_link' and not any(
                row['citation_key'] == str(link['citation_key']) and row.get('chapter_id') == link.get('chapter_id')
                and (not link.get('document_key') or any(origin['document_key']==link['document_key'] for origin in row.get('source_claims',[]))) for row in rows):
            rows.append({**link, 'reference_id': stable_hash(json.dumps(link, sort_keys=True), 24),
                         'identified': False, 'resolved': False, 'fetched': False, 'included_in_judgement': False,
                         'reason': 'Citation marker detected, but no unambiguous local bibliography entry', 'evidence_document_ids': []})
    return rows


def _evidence_level(route: str) -> str:
    return "source_reference" if route.startswith("source_citation") else "independent_external"


def _citation_reserved_order(ranked_results: list[tuple[float, tuple[dict[str, Any], Any, str]]],
                             reference_limit: int) -> list[tuple[float, tuple[dict[str, Any], Any, str]]]:
    """Reserve bounded leading slots for exact source-citation retrieval leads."""
    cited = [item for item in ranked_results
             if str(item[1][0].get("_retrieval_route", "")).startswith("source_citation")]
    broad = [item for item in ranked_results
             if not str(item[1][0].get("_retrieval_route", "")).startswith("source_citation")]
    return [*cited[:reference_limit], *broad, *cited[reference_limit:]]


def _independence_group(document: dict[str, Any]) -> str:
    """Conservative publication/study-family key for corroboration counts."""
    bibliography = _bibliographic_key(document)
    if bibliography:
        return bibliography
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


def _paper_material(result, identifiers, settings, cache):
    """Reuse parsed source text by exact access identity, independent of abstracts.

    Indexes can return different abstract strings for the same full-text copy.
    Those strings must not cause the same XML/PDF to be downloaded repeatedly.
    Unavailable access results are run-local only; abstract choice stays local
    to the metadata record. Parsed text is persisted in the bounded shared
    index when enabled; original PDF/XML bytes are transient.
    """
    access_diagnostics = result.setdefault('_access_diagnostics',[])
    for source, identifier in identifiers:
        check_cancelled()
        def fetch_xml():
            xml = fetch_fulltext_xml(source, identifier, settings)
            return {'paragraphs': extract_paragraphs(xml, with_sections=True) if xml else []}
        key = 'parsed-xml-table-v1:' + json.dumps([source, identifier])
        try:
            material = cache.get_or_compute(key, lambda: cache.persistent.material(key, fetch_xml)
                                           if cache.persistent else fetch_xml())
        except (OSError,ValueError) as exc:
            access_diagnostics.append({'kind':'xml','source':source,'status':'failed','error':type(exc).__name__})
            continue
        if material['paragraphs']:
            return material['paragraphs'], 'full_text'
    pdf_urls = list(dict.fromkeys([str(result.get('_open_access_pdf_url') or '').strip(),
                                  *result.get('_open_access_pdf_urls',[])]))
    for pdf_url in [url for url in pdf_urls if url][:4]:
        key = 'parsed-pdf-layout-v2:' + pdf_url
        def fetch_pdf():
            return {'paragraphs': paragraphs_from_pdf_pages(fetch_open_access_pdf_pages(pdf_url, settings))}
        try:
            material = cache.get_or_compute(key, lambda: cache.persistent.material(key, fetch_pdf)
                                           if cache.persistent else fetch_pdf())
        except (OSError,ValueError) as exc:
            access_diagnostics.append({'kind':'pdf','url':pdf_url,'status':'failed','error':type(exc).__name__})
            continue
        if material['paragraphs']:
            access_diagnostics.append({'kind':'pdf','url':pdf_url,'status':'available'})
            return material['paragraphs'], 'full_text'
        access_diagnostics.append({'kind':'pdf','url':pdf_url,'status':'no_usable_text'})
    abstract = str(result.get('abstractText') or '').strip()
    return ([abstract], 'abstract') if len(abstract) >= 80 else ([], 'unavailable')


def _search_index(provider: str, query: str, settings: Settings, cache: RunCache, articles_per_candidate: int):
    page_size = max(articles_per_candidate * 25, 100)
    key = f"search:{provider}:{page_size}:{query}"
    try:
        if provider == "europe_pmc":
            function = search
        elif provider == "openalex":
            function = search_openalex
        elif provider == "pubmed":
            function = search_pubmed
        else:
            function = search
        def find():
            saved = cache.persistent.get(key) if cache.persistent else None
            ttl = getattr(settings, 'search_cache_ttl_hours', 168) * 3600
            if saved and time.time() - saved.get('fetched_at', 0) < ttl:
                return saved['results']
            try:
                for search_attempt in range(3):
                    try:
                        found = function(query, settings, page_size)
                        break
                    except Exception:
                        if search_attempt == 2:
                            raise
                        time.sleep(0.5 * (search_attempt + 1))
                if cache.persistent:
                    cache.persistent.put(key, {'results': found, 'fetched_at': time.time()})
                return found
            except Exception:
                if saved and saved.get('results'):
                    return saved['results']
                raise
        return cache.get_or_compute(key, find), ""
    except (OSError, ValueError):
        return [], f"{provider}_search_unavailable"

def _load_material(entry: Any, settings: Settings, cache: RunCache):
    _score, (result, identifiers, _preview) = entry
    direct_pdf_url = str(result.get("_open_access_pdf_url") or "").strip()

    def fetch() -> tuple[list[Any], str]:
        material, access = _paper_material(result, identifiers, settings, cache)
        if material and cache.persistent:
            cache.persistent.catalogue(_result_key(result), result, material)
        return material, access

    # Run-local singleflight prevents repeated downloads; the shared index
    # also reuses successful parsed text across runs, within its TTL.
    return cache.get_or_compute("article:" + _result_key(result) + ":" + stable_hash(
        str((identifiers, direct_pdf_url, result.get('_open_access_pdf_urls',[]), result.get("abstractText"))), 32), fetch)

def _prepare_material(entry: Any, settings: Settings, cache: RunCache):
    began = time.monotonic()
    paragraphs, access = _load_material(entry, settings, cache)
    fetched = time.monotonic()
    # Chunk boundaries are claim-independent; reuse them across claims.
    key = 'chunks-index-v1:' + _result_key(entry[1][0]) + ':' + stable_hash(json.dumps(paragraphs, ensure_ascii=False), 32)
    def build_chunks():
        saved = cache.persistent.get(key, expiring=False) if cache.persistent else None
        if saved is not None:
            return saved['chunks']
        built = make_chunks(paragraphs)
        if cache.persistent:
            cache.persistent.put(key, {'chunks': built, 'paper_identity': _result_key(entry[1][0]),
                'sentence_index': [[{'number': n, 'text': sentence} for n, sentence in enumerate(
                    scholarly_sentences(c['text'] if isinstance(c, dict) else str(c)), 1)] for c in built]})
        return built
    chunks = cache.get_or_compute(key, build_chunks) if paragraphs else []
    return chunks, access, {'fetch_seconds': round(fetched - began, 3),
                            'chunk_seconds': round(time.monotonic() - fetched, 3)}

def _merge_metadata(records, notes):
    results: list[dict[str, Any]] = []
    seen_results: dict[str, dict[str, Any]] = {}
    seen_bibliography: dict[str, dict[str, Any]] = {}
    for result in records:
        key = _result_key(result)
        bibliography = _bibliographic_key(result)
        existing = seen_results.get(key) or (seen_bibliography.get(bibliography) if bibliography else None)
        if existing is not None:
            existing['_retrieval_target_ids'] = sorted(set(existing.get('_retrieval_target_ids', [])) | set(result.get('_retrieval_target_ids', [])))
            existing['_source_reference_ids'] = list(dict.fromkeys([*existing.get('_source_reference_ids', []), *result.get('_source_reference_ids', [])]))
            seen_results[key] = existing
            if bibliography:
                seen_bibliography[bibliography] = existing
            if key != _result_key(existing):
                notes.append({'article_id': key, 'reason': 'duplicate_publication_bibliography',
                              'retained_article_id': _result_key(existing)})
            # Preserve the higher-priority route while borrowing an explicit
            # OA PDF/landing page or abstract from the duplicate index record.
            for field in ("_open_access_pdf_url", "_landing_page_url", "_open_access_license", "abstractText"):
                if not existing.get(field) and result.get(field):
                    existing[field] = result[field]
            existing['_open_access_pdf_urls'] = list(dict.fromkeys([
                *existing.get('_open_access_pdf_urls',[]), *result.get('_open_access_pdf_urls',[]),
                *[r['_open_access_pdf_url'] for r in (existing,result) if r.get('_open_access_pdf_url')]]))[:4]
            if is_open_access(result):
                existing["isOpenAccess"] = "Y"
            incoming_rank = int(result.get("_query_rank") or 10_000)
            existing_rank = int(existing.get("_query_rank") or 10_000)
            if incoming_rank < existing_rank:
                existing["_query_rank"] = incoming_rank
            continue
        enriched = dict(result)
        if not enriched.get("_open_access_pdf_url"):
            enriched["_open_access_pdf_url"] = europe_pmc_open_access_pdf(enriched)
        seen_results[key] = enriched
        if bibliography:
            seen_bibliography[bibliography] = enriched
        results.append(enriched)
    return results

def _accessible_results(candidate, results, notes):
    seen: set[str] = set()
    eligible: list[tuple[dict[str, Any], list[tuple[str, str]], str]] = []
    for result in results:
        same_source_reason = next((reason for source in candidate.get('originating_sources', [candidate])
                                   if (reason := originating_source_match(source, result))), None)
        if same_source_reason:
            notes.append({"article_id": str(result.get("id", "")), "reason": same_source_reason})
            continue
        identifiers = fulltext_identifiers(result) if is_open_access(result) else []
        direct_pdf_url = str(result.get("_open_access_pdf_url") or "").strip()
        abstract = str(result.get("abstractText") or "").strip()
        if not identifiers and not direct_pdf_url and len(abstract) < 80:
            notes.append({'article_id': _result_key(result), 'reason': 'metadata_only_no_accessible_evidence'})
            continue
        article_key = "external-" + stable_hash(f"{result.get('source')}|{result.get('id')}|{result.get('pmcid')}|{result.get('doi')}", 32)
        if article_key in seen:
            continue
        seen.add(article_key)
        preview = " ".join(str(result.get(field, "") or "") for field in ("title", "abstractText", "journalTitle"))
        eligible.append((result, identifiers, preview))

    return eligible

def retrieve_round(candidate: dict[str, Any], client: OpenCodeClient, reranker: SemanticReranker, settings: Settings,
                   articles_per_candidate: int, top_chunks: int,
                   progress: Callable[[str], None] | None = None,
                   query_translation: dict[str, Any] | None = None,
                   cache: RunCache | None = None, on_document=None) -> dict[str, Any]:
    check_cancelled()
    started_at = time.monotonic()
    raw_progress = progress
    progress = (lambda message: raw_progress(message if isinstance(message, TaggedMessage) else
                TaggedMessage(message, 'literature_search', candidate['candidate_id']))) if raw_progress else None
    def report(stage, message):
        if raw_progress:
            raw_progress(TaggedMessage(message, stage, candidate['candidate_id']))
    translated = translate_query(client, candidate) if query_translation is None else query_translation
    if translated.get("batch_error"):
        translated = {"query": "", "query_strategy": "literal_fallback", "planning_error": translated.get("rationale")}
    query_plan = ({provider: list(dict.fromkeys(row[field] for row in translated['queries']))
                   for provider, field in [('europe_pmc', 'query'), ('openalex', 'openalex_query'), ('pubmed', 'query')]}
                  if candidate.get('pathway_target') else {**evidence_query_plan(candidate, translated),
                                                          'pubmed': evidence_query_plan(candidate, translated).get('europe_pmc', [])})
    if progress:
        progress(f"Europe PMC queries: {' | '.join(query_plan['europe_pmc'])}")
    hypothesis = candidate.get('pathway_description') or generate_hypothesis(candidate)
    # Search a broader metadata-only pool, then let BGE choose which few papers
    # merit transient full-text retrieval.
    notes: list[dict[str, str]] = []
    broad_results: list[dict[str, Any]] = []
    europe_query_counts: list[dict[str, Any]] = []
    openalex_results: list[dict[str, Any]] = []
    openalex_query_counts: list[dict[str, Any]] = []
    pubmed_results: list[dict[str, Any]] = []
    pubmed_query_counts: list[dict[str, Any]] = []
    cache = cache if cache is not None else RunCache()

    citation_ledger = citation_tracking(candidate)


    # Interleave sources so one slow index cannot occupy every initial slot.
    providers = ('europe_pmc', 'openalex', 'pubmed')
    search_plan = [(provider, query) for n in range(max(len(query_plan.get(p, [])) for p in providers))
                   for provider in providers for query in query_plan.get(provider, [])[n:n+1]]
    tasks = [(f'search-{i}', _search_index, (provider, query, settings, cache, articles_per_candidate)) for i, (provider, query) in enumerate(search_plan)]
    references = candidate.get('source_references') or []
    tasks += [(f'citation-{i}', _citation_results, ({**candidate, 'source_references': [reference]}, settings, notes, cache, citation_ledger[i]))
              for i, reference in enumerate(references)]
    completed_searches, deferred = collect_searches(tasks, settings.retrieval_workers,
        settings.search_wait_seconds if settings.external_judgement_mode in {'trust_graph', 'pathway_only'} else float('inf'))
    pending_searches = []
    for i, (provider, query) in enumerate(search_plan):
        reply, failure = completed_searches.get(f'search-{i}', (None, 'deadline'))
        found, error = reply if reply is not None else ([], failure)
        if error:
            notes.append({'article_id': f'{provider}-broad-search', 'reason': error})
            pending_searches.append({'kind': 'search', 'provider': provider, 'query': query, 'reason': error})
        if provider == 'europe_pmc':
            target, counts = broad_results, europe_query_counts
        elif provider == 'openalex':
            target, counts = openalex_results, openalex_query_counts
        else:
            target, counts = pubmed_results, pubmed_query_counts
        facet_ids = [r['target_id'] for r in translated.get('queries', [])
                     if r.get('query' if provider in {'europe_pmc', 'pubmed'} else 'openalex_query') == query]
        target.extend({**result, '_query_rank': rank, '_retrieval_target_ids': facet_ids} for rank, result in enumerate(found, 1))
        counts.append({'query': query, 'result_count': len(found), 'status': 'pending' if error else 'complete'})
    cited_results = []
    for i, reference in enumerate(references):
        found, error = completed_searches.get(f'citation-{i}', (None, 'deadline'))
        if error:
            pending_searches.append({'kind': 'citation', 'reference_id': citation_ledger[i]['reference_id'], 'reason': error})
            citation_ledger[i].update(status='pending', reason='citation_lookup_pending')
        else:
            cited_results.extend(found)
    if pending_searches and progress:
        progress(TaggedMessage(f'Retrieval pending: {len(pending_searches)} searches or citations did not complete; available papers continue, resume retries missing sources', 'external_validation'))
    search_seconds = time.monotonic() - started_at
    local_results = cache.persistent.search_papers(' '.join(str(candidate.get(k) or '') for k in ('subject', 'object'))) if cache.persistent else []
    results = _merge_metadata([*cited_results, *broad_results, *openalex_results, *pubmed_results, *local_results], notes)
    if progress:
        progress(f"Literature search returned {len(broad_results)} Europe PMC results, "
                 f"{len(openalex_results)} OpenAlex results, and {len(cited_results)} cited-paper matches; "
                 f"selecting all usable cited leads plus up to {articles_per_candidate} search papers")
    documents: list[dict[str, Any]] = []
    eligible = _accessible_results(candidate, results, notes)

    # Rank search-result titles/abstracts before downloading XML. This prevents
    # the first open-access hits from crowding out more relevant papers while
    # retaining exact parsed material in the shared index when enabled.
    ranked_at = time.monotonic()
    report('article_ranking', f"Ranking articles: {len(eligible)} accessible metadata records")
    if candidate.get('pathway_target'):
        from backend.stages.evidence_ranking.pathways import balanced_articles
        ranked_results = balanced_articles(candidate, eligible, reranker)
    else:
        ranked_results = _hybrid_order(hypothesis, candidate, eligible, reranker)
    article_ranking_seconds = time.monotonic() - ranked_at
    reference_documents_selected = 0
    independent_documents_selected = 0
    reference_limit = sum(str(entry[1][0].get("_retrieval_route", "")).startswith("source_citation") for entry in ranked_results)
    # All resolvable citations attached to the source evidence receive slots
    # outside the independent-search budget. Their provenance remains explicit.
    selection_order = _citation_reserved_order(ranked_results, reference_limit)


    references = [entry for entry in selection_order if str(entry[1][0].get("_retrieval_route", "")).startswith("source_citation")]
    independent = [entry for entry in selection_order if not str(entry[1][0].get("_retrieval_route", "")).startswith("source_citation")]
    pdf_only = [entry for entry in independent if len(str(entry[1][0].get('abstractText') or '')) < 80]
    reserved = pdf_only[:max(1, articles_per_candidate // 4)] if not candidate.get('pathway_target') else []
    independent = reserved + [entry for entry in independent if all(entry is not chosen for chosen in reserved)]
    attempts = 0
    max_attempts = max(8, (articles_per_candidate + reference_limit) * 3)
    materialized = []
    preparation_timings = []
    fetch_rank_started = time.monotonic()


    def prepared_waves():
        nonlocal attempts
        with ThreadPoolExecutor(max_workers=settings.retrieval_workers, thread_name_prefix='article-prepare') as executor:
            remaining_references, remaining_independent = list(references), list(independent)
            while attempts < max_attempts:
                check_cancelled()
                reference_needed = max(0, reference_limit - sum(str(e[0][1][0].get('_retrieval_route', '')).startswith('source_citation') for e in materialized))
                independent_needed = max(0, articles_per_candidate - sum(not str(e[0][1][0].get('_retrieval_route', '')).startswith('source_citation') for e in materialized))
                wave = (remaining_references[:reference_needed] + remaining_independent[:independent_needed])[:max_attempts - attempts]
                remaining_references = remaining_references[reference_needed:]
                remaining_independent = remaining_independent[independent_needed:]
                if not wave:
                    break
                report('article_fetch', f'Fetching and preparing {len(wave)} papers with {settings.retrieval_workers} workers')
                pending = {submit_work(executor, _prepare_material, entry, settings, cache): (i, entry) for i, entry in enumerate(wave)}
                attempts += len(pending)
                completed = []
                for future in as_completed(pending):
                    check_cancelled()
                    order, entry = pending[future]
                    try:
                        chunks, access, timings = future.result()
                    except (OSError, ValueError) as exc:
                        notes.append({'article_id': _result_key(entry[1][0]), 'reason': f'article_access_failed: {exc}'})
                        continue
                    if chunks:
                        # GPU/model inference has one owner. CPU parsing/chunking
                        # continues on fetch workers while this paper is ranked.
                        completed.append((order, entry, chunks, access))
                        yield entry, chunks, access, timings
                    else:
                        notes.append({'article_id': _result_key(entry[1][0]), 'reason': 'full_text_and_abstract_unavailable',
                                      'access_diagnostics':entry[1][0].get('_access_diagnostics',[])})
                materialized.extend((entry, chunks, access) for _, entry, chunks, access in sorted(completed))

    neutral_query = ' '.join(str(candidate.get(key) or '') for key in ('subject', 'object')) + ' ' + ' '.join(
        str(value) for value in (candidate.get('qualifiers') or {}).values() if value)
    for (article_score, (result, identifiers, _preview)), all_chunks, evidence_access, timings in prepared_waves():
        route = str(result.get('_retrieval_route', 'broad_search'))
        is_source_reference = route.startswith('source_citation')
        article_key = 'external-' + stable_hash(f"{result.get('source')}|{result.get('id')}|{result.get('pmcid')}|{result.get('doi')}", 32)
        began = time.monotonic()
        if candidate.get('pathway_target'):
            from backend.stages.evidence_ranking.pathways import balanced_passages
            chunks = balanced_passages(candidate, all_chunks, article_key, top_chunks, reranker)
        else:
            chunks = reranker.select(hypothesis, all_chunks, article_key, top_chunks, neutral_query=neutral_query)
        timings.update(article_id=article_key, candidate_chunks=len(all_chunks),
                       ranking_seconds=round(time.monotonic() - began, 3))
        preparation_timings.append(timings)
        if not chunks:
            notes.append({'article_id': article_key, 'reason': 'no_paragraphs_for_semantic_reranking'})
            continue
        document = {**article_metadata(result, article_key), "article_semantic_score": round(article_score, 6),
                          "rrf_score": round(float(result.get("_rrf_score") or 0.0), 6),
                          "article_cross_encoder_score": round(float(result.get("_article_cross_encoder_score") or 0.0), 6),
                          "hybrid_retrieval_score": round(article_score, 6),
                          "retrieval_route": route,
                          'access_diagnostics':result.get('_access_diagnostics',[]),
                          'retrieval_target_ids': result.get('_retrieval_target_ids', []),
                          **({'pathway_target_ranks': result.get('_pathway_target_ranks', {})} if candidate.get('pathway_target') else {}),
                          "evidence_level": _evidence_level(route),
                          "source_reference_number": result.get("_source_reference_number"),
                          'source_reference_text': result.get('_source_reference_text'),
                          'resolution_method': result.get('_resolution_method'),
                          'source_reference_ids': result.get('_source_reference_ids', []),
                          'passage_selection': {'query': hypothesis, 'neutral_query': neutral_query, 'candidate_chunks': len(all_chunks),
                                                'retained_chunks': len(chunks), 'requested_chunks': top_chunks},
                          "evidence_access": evidence_access,
                          "evidence_chunks": chunks}
        document["independence_group"] = _independence_group(document)
        documents.append(document)
        if on_document:
            on_document(document)
        for row in citation_ledger:
            if row['reference_id'] in document['source_reference_ids']:
                row.update(fetched=True, status='fetched', evidence_access=evidence_access,
                           evidence_document_ids=[*row['evidence_document_ids'], article_key])
        if is_source_reference:
            reference_documents_selected += 1
        else:
            independent_documents_selected += 1
        if progress:
            report('chunk_preparation', f"Ranked {len(chunks)} passages: {result.get('title', 'Untitled article')}")
    order_by_key = {_result_key(entry[1][0]): index for index, entry in enumerate(selection_order)}
    # Completion order must never affect prompt order or paper allocation.
    article_order = {'external-' + stable_hash(f"{entry[1][0].get('source')}|{entry[1][0].get('id')}|{entry[1][0].get('pmcid')}|{entry[1][0].get('doi')}", 32): order_by_key[_result_key(entry[1][0])] for entry in selection_order}
    documents.sort(key=lambda d: article_order[d['evidence_document_id']])
    report('chunk_preparation', f"Passage preparation completed: {len(documents)} papers; {time.monotonic() - fetch_rank_started:.1f}s fetch/prepare/rank wall time")
    for row in citation_ledger:
        if row['resolved'] and not row['fetched'] and row['status'] != 'pending':
            row.update(status='unavailable', reason=row['reason'] or 'No usable text/chunks retrieved; see retrieval notes')
    return {"query_translation": translated, "query_plan": query_plan, 'citation_ledger': citation_ledger,
            'retrieval_pending': bool(pending_searches), 'pending_sources': pending_searches,
            'retrieval_coverage': {'unique_metadata_records': len(results), 'accessible_candidates': len(eligible),
                                   'missing_abstract_candidates': sum(len(str(e[0].get('abstractText') or '')) < 80 for e in eligible),
                                   'fetch_attempts': attempts, 'papers_retained': len(documents),
                                   'independent_paper_budget': articles_per_candidate,
                                   'eligible_not_fetched': max(0, len(eligible) - attempts),
                                   'complete_literature_coverage': False},
            "query_result_counts": {"europe_pmc": europe_query_counts, "openalex": openalex_query_counts, "pubmed": pubmed_query_counts},
            "retrieval_hypothesis": hypothesis,
            "result_count": len(broad_results) + len(openalex_results) + len(pubmed_results),
            "europe_pmc_result_count": len(broad_results),
            "openalex_result_count": len(openalex_results),
            "pubmed_result_count": len(pubmed_results),
            'shared_index_result_count': len(local_results),
            "cited_result_count": len(cited_results),
            "evidence_documents_count": len(documents),
            "open_access_evidence_documents": sum(document.get("evidence_access") == "full_text" for document in documents),
            "paper_preparation_timings": preparation_timings,
            "timings_seconds": {"article_ranking": round(article_ranking_seconds, 3),
                                "fetch_prepare_rank": round(time.monotonic() - fetch_rank_started, 3), "search": round(search_seconds, 3),
                                "retrieval_total": round(time.monotonic() - started_at, 3)},
            "notes": notes, "evidence_documents": documents}
