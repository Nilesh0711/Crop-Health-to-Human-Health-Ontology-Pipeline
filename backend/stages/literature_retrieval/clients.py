"""Europe PMC, Crossref, and OpenAlex access; external full text stays in memory."""

from __future__ import annotations

import json
import io
import html
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import time
from backend.shared.cancellation import check_cancelled, current_token
from typing import Any

from pypdf import PdfReader

from backend.config.settings import Settings
from backend.shared.text_structure import REFERENCE_HEADING_TEXT
from backend.shared.metadata_http import MetadataError


SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
FULLTEXT_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{source}/{article_id}/fullTextXML"
FULLTEXT_BY_ID_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{article_id}/fullTextXML"
CROSSREF_URL = "https://api.crossref.org/works"
OPENALEX_WORK_URL = "https://api.openalex.org/works/{doi_url}"
OPENALEX_SEARCH_URL = "https://api.openalex.org/works"
PUBMED_SEARCH_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esearch.fcgi"
PUBMED_SUMMARY_URL = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi"
UNPAYWALL_WORK_URL = "https://api.unpaywall.org/v2/{doi}"
_REFERENCE_STOPWORDS = {"a", "an", "and", "the", "of", "for", "in", "on", "to", "with", "from", "by", "i", "b", "em", "sub", "sup", "lt", "gt"}


_OPENALEX_QUOTA_EXHAUSTED = False


def fetch_json(url: str, settings: Settings) -> dict[str, Any]:
    """Read scholarly metadata with a bounded retry for transient APIs.

    This retries availability and rate-limit failures only. It never repeats a
    model verdict, changes a query, or turns absent evidence into support.
    """
    check_cancelled()
    if current_token() is not None:
        from backend.shared.metadata_http import request_json
        return request_json('GET', url, headers={'User-Agent': settings.user_agent, 'Accept': 'application/json'},
                            timeout=settings.metadata_timeout_seconds, max_bytes=settings.max_fulltext_bytes)
    request = urllib.request.Request(url, headers={"User-Agent": settings.user_agent, "Accept": "application/json"})
    for attempt in range(2):
        check_cancelled()
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read(settings.max_fulltext_bytes).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and 'api.openalex.org' in url:
                global _OPENALEX_QUOTA_EXHAUSTED
                _OPENALEX_QUOTA_EXHAUSTED = True
                raise
            if exc.code not in {429, 500, 502, 503, 504} or attempt:
                raise
            time.sleep(0.6)
    raise RuntimeError("unreachable metadata retrieval state")


def search(query: str, settings: Settings, page_size: int) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"query": query, "format": "json", "pageSize": page_size, "resultType": "core"})
    return fetch_json(f"{SEARCH_URL}?{params}", settings).get("resultList", {}).get("result", [])


def resolve_bibliographic_reference(reference: dict[str, Any], settings: Settings, diagnostics=None) -> dict[str, Any] | None:
    """Resolve a source-paper bibliography entry without treating it as evidence."""
    def fail(reason):
        if diagnostics is not None:
            diagnostics.update(reason=reason)
        return None
    raw_citation = str(reference.get("citation_text") or "").strip()
    citation = re.sub(r"([A-Za-z]{2,})-\s+([a-z]{2,})", r"\1\2", raw_citation)
    known_doi = str(reference.get("doi") or "").strip()
    if not known_doi:
        doi_match = re.search(r'10\.\d{4,9}/[^\s<>"\]]+', citation, re.IGNORECASE)
        known_doi = doi_match.group(0).rstrip('.,;)') if doi_match else ''
    if known_doi:
        known_doi = re.sub(r'^https?://(?:dx\.)?doi\.org/','',known_doi,flags=re.I)
        if not re.fullmatch(r'10\.\d{4,9}/\S+',known_doi):
            return fail('malformed_doi')
        return {"doi": known_doi, "title": "", "reference_number": reference.get("reference_number"), "resolution_method": "bibliography_doi"}
    if not citation:
        return fail('missing_reference_text')
    params = urllib.parse.urlencode({"query.bibliographic": citation[:1500], "rows": 3,
                                     "select": "DOI,title,author,published-print,published-online,container-title"})
    try:
        items = fetch_json(f"{CROSSREF_URL}?{params}", settings).get("message", {}).get("items", [])
    except MetadataError as exc:
        if exc.status in {404, 410}:
            return fail('reference_not_indexed')
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        if diagnostics is not None:
            raise  # Retrieval scheduling records a pending technical lookup, not absent evidence.
        return fail('reference_lookup_failed')
    if not items:
        return fail('no_reference_candidates')
    citation_terms = set(re.findall(r"[a-z0-9]+", citation.casefold()))
    # Crossref's top result is only retained when its title is visibly present
    # in the local bibliography entry. This prevents a fuzzy resolver result
    # from silently becoming a cited evidence paper.
    matches = {}
    for item in items:
        value = item.get('title') or []
        title = str(value[0] if isinstance(value,list) and value else value or '').strip()
        clean_title = re.sub(r'<[^>]+>', ' ', html.unescape(title))
        terms = {t for t in re.findall(r'[a-z0-9]+', clean_title.casefold()) if t not in _REFERENCE_STOPWORDS and len(t) > 1}
        authors = item.get('author') or []
        family = str(authors[0].get('family') or '').casefold() if authors else ''
        years = {str(y) for key in ('published-print', 'published-online', 'issued')
                 for dates in (item.get(key) or {}).get('date-parts', []) for y in dates[:1]}
        doi = str(item.get('DOI') or '').strip().casefold()
        overlap = len(terms & citation_terms) / len(terms) if terms else 0.0
        title_matches = (len(terms) >= 3 and (overlap >= 0.85 or (len(terms) >= 6 and len(terms - citation_terms) <= 1)))
        if (title_matches and family and family in citation.casefold() and years.intersection(re.findall(r'\b(?:19|20)\d{2}\b', citation)) and doi):
            matches[doi] = clean_title
    if len(matches)!=1:
        return fail('ambiguous_reference_match' if matches else 'title_author_year_mismatch')
    doi,title = next(iter(matches.items()))
    return {"doi": doi, "title": title,
            "reference_number": reference.get("reference_number"), "resolution_method": "crossref_title_author_year"}


def openalex_pdf_locations(work):
    locations = [work.get('best_oa_location') or {}, work.get('primary_location') or {}, *(work.get('locations') or [])]
    return list(dict.fromkeys(str(loc['pdf_url']).strip() for loc in locations
        if isinstance(loc,dict) and loc.get('pdf_url') and (loc == work.get('best_oa_location') or loc.get('is_oa')
                                  or (work.get('open_access') or {}).get('is_oa'))))[:4]


def unpaywall_open_access_copy(doi: str, settings: Settings) -> dict[str, Any] | None:
    """Return open-access location and PDF URL from Unpaywall (free, legal OA aggregator)."""
    normalized_doi = str(doi or "").strip().casefold()
    if not normalized_doi:
        return None
    email = getattr(settings, 'openalex_email', '') or 'nilesh.cs25@kgpian.iitkgp.ac.in'
    url = f"{UNPAYWALL_WORK_URL.format(doi=urllib.parse.quote(normalized_doi))}?email={urllib.parse.quote(email)}"
    try:
        data = fetch_json(url, settings)
    except (MetadataError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get('is_oa'):
        return None
    best = data.get('best_oa_location') or {}
    pdf_url = str(best.get('url_for_pdf') or '').strip()
    landing_url = str(best.get('url') or data.get('doi_url') or '').strip()
    pdf_urls = [pdf_url] if pdf_url else []
    for loc in data.get('oa_locations') or []:
        if isinstance(loc, dict) and loc.get('url_for_pdf'):
            u = str(loc['url_for_pdf']).strip()
            if u and u not in pdf_urls:
                pdf_urls.append(u)
    return {
        "source": "UNPAYWALL",
        "id": normalized_doi,
        "doi": normalized_doi,
        "title": str(data.get("title") or "").strip(),
        "journalTitle": str(data.get("journal_name") or "").strip(),
        "pubYear": str(data.get("year") or "").strip(),
        "authorString": "",
        "abstractText": "",
        "isOpenAccess": "Y",
        "_open_access_pdf_url": pdf_url or (pdf_urls[0] if pdf_urls else landing_url),
        "_open_access_pdf_urls": pdf_urls,
        "_landing_page_url": landing_url,
        "_open_access_license": str(best.get("license") or "").strip(),
        "_retrieval_route": "unpaywall_oa_lookup",
    }


def search_pubmed(query: str, settings: Settings, page_size: int) -> list[dict[str, Any]]:
    """Search PubMed via NCBI Entrez E-utilities alongside Europe PMC and OpenAlex."""
    email = getattr(settings, 'openalex_email', '') or 'nilesh.cs25@kgpian.iitkgp.ac.in'
    search_params = urllib.parse.urlencode({
        'db': 'pubmed',
        'term': query,
        'retmode': 'json',
        'retmax': min(max(1, page_size), 100),
        'email': email,
        'tool': 'crop_health_workbench'
    })
    try:
        search_data = fetch_json(f"{PUBMED_SEARCH_URL}?{search_params}", settings)
    except (MetadataError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        return []
    id_list = search_data.get('esearchresult', {}).get('idlist', [])
    if not id_list:
        return []
    sum_params = urllib.parse.urlencode({
        'db': 'pubmed',
        'id': ','.join(id_list),
        'retmode': 'json',
        'email': email,
        'tool': 'crop_health_workbench'
    })
    try:
        sum_data = fetch_json(f"{PUBMED_SUMMARY_URL}?{sum_params}", settings)
    except (MetadataError, urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        return []
    result_dict = sum_data.get('result', {})
    results: list[dict[str, Any]] = []
    for pmid in id_list:
        item = result_dict.get(pmid)
        if not isinstance(item, dict):
            continue
        doi = ''
        pmc = ''
        for aid in item.get('articleids', []):
            if isinstance(aid, dict):
                if aid.get('idtype') == 'doi':
                    doi = str(aid.get('value') or '').casefold()
                elif aid.get('idtype') == 'pmc':
                    pmc = str(aid.get('value') or '')
        authors = [str(a.get('name') or '') for a in item.get('authors', []) if isinstance(a, dict) and a.get('name')]
        results.append({
            'source': 'PUBMED',
            'id': pmid,
            'doi': doi,
            'pmcid': pmc,
            'title': str(item.get('title') or '').rstrip('.'),
            'journalTitle': str(item.get('source') or ''),
            'pubYear': str((item.get('pubdate') or '')[:4]),
            'authorString': ', '.join(authors[:8]),
            'abstractText': '',
            'isOpenAccess': 'Y' if pmc else 'N',
            '_retrieval_route': 'pubmed_broad_search',
        })
    return results


def openalex_open_access_copy(doi: str, settings: Settings) -> dict[str, Any] | None:
    """Return usable OpenAlex evidence metadata for a cited DOI.

    A public PDF is preferred, but an indexed abstract is also usable evidence.
    Both still pass passage reranking and entailment; the bibliography/metadata
    record alone never counts as support. Public PDF bytes remain transient.
    """
    global _OPENALEX_QUOTA_EXHAUSTED
    if _OPENALEX_QUOTA_EXHAUSTED and not getattr(settings, 'openalex_api_key', None):
        return None
    normalized_doi = str(doi or "").strip().casefold()
    if not normalized_doi:
        return None
    doi_url = urllib.parse.quote(f"https://doi.org/{normalized_doi}", safe="")
    oa_url = OPENALEX_WORK_URL.format(doi_url=doi_url)
    auth_params = {}
    if getattr(settings, 'openalex_api_key', None):
        auth_params['api_key'] = settings.openalex_api_key
    if getattr(settings, 'openalex_email', None):
        auth_params['mailto'] = settings.openalex_email
    if auth_params:
        oa_url += f"?{urllib.parse.urlencode(auth_params)}"
    try:
        work = fetch_json(oa_url, settings)
    except MetadataError as exc:
        if exc.status in {404, 410}:
            return None
        raise
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        return None
    actual_doi = str(work.get('doi') or '').strip().casefold().removeprefix('https://doi.org/')
    if actual_doi and actual_doi != normalized_doi:
        return None
    location = work.get("best_oa_location") or work.get("primary_location") or {}
    pdf_urls = openalex_pdf_locations(work)
    pdf_url = pdf_urls[0] if pdf_urls else ''
    landing_page_url = str(location.get("landing_page_url") or "").strip()
    abstract = _openalex_abstract(work.get("abstract_inverted_index"))
    if not pdf_url and len(abstract) < 80:
        return None
    return {
        "source": "OPENALEX",
        "id": str(work.get("id") or normalized_doi),
        "doi": normalized_doi,
        "title": str(work.get("title") or "").strip(),
        "journalTitle": str((location.get("source") or {}).get("display_name") or "").strip(),
        "pubYear": str(work.get("publication_year") or "").strip(),
        "authorString": "",
        "abstractText": abstract,
        "isOpenAccess": "Y" if bool((work.get("open_access") or {}).get("is_oa")) else "N",
        "_open_access_pdf_url": pdf_url,
        "_open_access_pdf_urls": pdf_urls,
        "_landing_page_url": landing_page_url,
        "_open_access_license": str(location.get("license") or "").strip(),
    }


def _openalex_abstract(inverted_index: object) -> str:
    """Reconstruct an OpenAlex abstract from its public inverted index."""
    if not isinstance(inverted_index, dict):
        return ""
    positioned: list[tuple[int, str]] = []
    for word, positions in inverted_index.items():
        if not isinstance(positions, list):
            continue
        positioned.extend((position, str(word)) for position in positions if isinstance(position, int))
    return " ".join(word for _position, word in sorted(positioned))


def search_openalex(query: str, settings: Settings, page_size: int) -> list[dict[str, Any]]:
    """Search agronomy-friendly OpenAlex metadata alongside Europe PMC.

    OpenAlex is not treated as evidence merely because it indexed a work. Its
    abstract or transient public PDF must still pass passage reranking and the
    exact model and anchor checks used for every external document.
    """
    global _OPENALEX_QUOTA_EXHAUSTED
    if _OPENALEX_QUOTA_EXHAUSTED and not getattr(settings, 'openalex_api_key', None):
        raise OSError("openalex_search_rate_limited")
    params_dict = {
        "search": query,
        "select": ("id,doi,title,publication_year,authorships,abstract_inverted_index,"
                   "open_access,best_oa_location,primary_location,locations"),
        "per-page": min(max(1, page_size), 200),
    }
    if getattr(settings, 'openalex_api_key', None):
        params_dict['api_key'] = settings.openalex_api_key
    if getattr(settings, 'openalex_email', None):
        params_dict['mailto'] = settings.openalex_email
    params = urllib.parse.urlencode(params_dict)
    try:
        payload = fetch_json(f"{OPENALEX_SEARCH_URL}?{params}", settings)
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            _OPENALEX_QUOTA_EXHAUSTED = True
        raise
    results: list[dict[str, Any]] = []
    for work in payload.get("results", []):
        if not isinstance(work, dict):
            continue
        location = work.get("best_oa_location") or {}
        primary_location = work.get("primary_location") or {}
        source = location.get("source") or primary_location.get("source") or {}
        doi = str(work.get("doi") or "")
        doi = re.sub(r"^https?://(?:dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
        authorships = work.get("authorships") or []
        authors = [str((authorship.get("author") or {}).get("display_name") or "").strip()
                   for authorship in authorships if isinstance(authorship, dict)]
        pdf_urls = openalex_pdf_locations(work)
        pdf_url = pdf_urls[0] if pdf_urls else ''
        landing_url = str(location.get("landing_page_url") or work.get("id") or "").strip()
        results.append({
            "source": "OPENALEX",
            "id": str(work.get("id") or doi),
            "doi": doi.casefold(),
            "title": str(work.get("title") or "").strip(),
            "journalTitle": str(source.get("display_name") or "").strip(),
            "pubYear": str(work.get("publication_year") or "").strip(),
            "authorString": ", ".join(author for author in authors[:8] if author),
            "abstractText": _openalex_abstract(work.get("abstract_inverted_index")),
            "isOpenAccess": "Y" if bool((work.get("open_access") or {}).get("is_oa")) else "N",
            "_open_access_pdf_url": pdf_url,
            "_open_access_pdf_urls": pdf_urls,
            "_landing_page_url": landing_url,
            "_open_access_license": str(location.get("license") or "").strip(),
            "_retrieval_route": "openalex_broad_search",
        })
    return results


def is_open_access(result: dict[str, Any]) -> bool:
    return (str(result.get("isOpenAccess", "")).upper() in {"Y", "YES", "TRUE", "1"}
            or bool(europe_pmc_open_access_pdf(result)))


def europe_pmc_open_access_pdf(result: dict[str, Any]) -> str:
    """Return an explicitly public PDF exposed by Europe PMC metadata."""
    values = (result.get("fullTextUrlList") or {}).get("fullTextUrl", [])
    if isinstance(values, dict):
        values = [values]
    for item in values if isinstance(values, list) else []:
        if not isinstance(item, dict):
            continue
        url = str(item.get("url") or "").strip()
        if (str(item.get("availabilityCode") or "").upper() == "OA"
                and str(item.get("documentStyle") or "").casefold() == "pdf"
                and url.startswith(("https://", "http://"))):
            return url
    return ""


def fulltext_identifiers(result: dict[str, Any]) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    if result.get("pmcid"):
        pairs.append(("PMC", str(result["pmcid"])))
    if result.get("source") and result.get("id"):
        pairs.append((str(result["source"]), str(result["id"])))
    return list(dict.fromkeys(pairs))


def fetch_fulltext_xml(source: str, article_id: str, settings: Settings) -> str | None:
    check_cancelled()
    quoted_source, quoted_id = urllib.parse.quote(source, safe=""), urllib.parse.quote(article_id, safe="")
    urls = [FULLTEXT_URL.format(source=quoted_source, article_id=quoted_id), FULLTEXT_BY_ID_URL.format(article_id=quoted_id)]
    for url in urls:
        request = urllib.request.Request(url, headers={"User-Agent": settings.user_agent, "Accept": "application/xml,text/xml"})
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                payload = response.read(settings.max_fulltext_bytes + 1)
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
            continue
        if len(payload) <= settings.max_fulltext_bytes:
            return payload.decode("utf-8", errors="replace")
    return None


def fetch_open_access_pdf_pages(url: str, settings: Settings) -> list[dict[str, Any]]:
    """Extract page-aware text from one public PDF, bounded and in memory.

    The caller supplies an OpenAlex OA PDF URL.  The bytes and extracted text
    live only for passage selection and are discarded immediately afterwards.
    """
    check_cancelled()
    if not str(url or "").startswith(("https://", "http://")):
        return []
    # Public metadata can contain literal spaces/non-ASCII in PDF paths.
    # Encode those without double-encoding percent escapes or URL delimiters.
    url = urllib.parse.quote(str(url), safe=":/?#[]@!$&'()*+,;=%")
    request = urllib.request.Request(url, headers={
        "User-Agent": settings.user_agent,
        "Accept": "application/pdf",
    })
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read(settings.max_fulltext_bytes + 1)
            content_type = str(response.headers.get("Content-Type") or "").casefold()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        return []
    if len(payload) > settings.max_fulltext_bytes or ("pdf" not in content_type and not payload.startswith(b"%PDF")):
        return []
    try:
        reader = PdfReader(io.BytesIO(payload))
        pages: list[dict[str, Any]] = []
        references_started = False
        for page_number, page in enumerate(reader.pages[:80], 1):
            from backend.stages.pdf_parsing.layout import page_text
            raw, _layout = page_text(page)
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
            kept: list[str] = []
            for line in lines:
                if re.match(rf"^(?:\d+(?:\.\d+)*\.?)?\s*{REFERENCE_HEADING_TEXT}\s*$", line, re.IGNORECASE):
                    references_started = True
                    break
                if re.match(r"^(?:acknowledg(?:e)?ments?|author contributions|funding|conflicts? of interest)\s*$",
                            line, re.IGNORECASE):
                    break
                kept.append(line)
            if references_started:
                break
            text = normalize_text(" ".join(kept))
            if len(text) >= 80:
                pages.append({"page_number": page_number, "text": text})
    except Exception:
        return []
    return pages


def paragraphs_from_plain_text(text: str) -> list[str]:
    """Turn transient PDF text into conservative paragraph candidates."""
    pieces = [normalize_text(piece) for piece in re.split(r"\n{2,}|(?<=[.!?])\s{2,}", str(text or ""))]
    return [piece for piece in pieces if len(piece) >= 80]


def paragraphs_from_pdf_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create page-aware paragraph records after bibliography/caption removal."""
    paragraphs: list[dict[str, Any]] = []
    for page in pages:
        for text in paragraphs_from_plain_text(str(page.get("text") or "")):
            caption = re.match(r"^(figure|table)\s+\d+\s*[.:]", text, re.IGNORECASE)
            paragraphs.append({"page_number": page.get("page_number"), "text": text,
                               **({'source_kind': 'pdf_caption', 'layout_verified': False} if caption else {})})
    return paragraphs


def _xml_table_and_figure_blocks(root):
    """Explicit source-cell serialization, retaining spans instead of guessing headers."""
    blocks = []
    for number, table in enumerate(root.findall('.//table-wrap'), 1):
        identifier = table.get('id') or f'table-{number}'
        caption = normalize_text(' '.join(table.find('caption').itertext())) if table.find('caption') is not None else ''
        header_rows = []
        for row in table.findall('.//thead/tr'):
            header_rows.append([{'text': normalize_text(' '.join(cell.itertext())),
                                 'colspan': cell.get('colspan', '1'), 'rowspan': cell.get('rowspan', '1')}
                                for cell in row if cell.tag in {'th', 'td'}])
        notes = [normalize_text(' '.join(n.itertext())) for n in table.findall('.//table-wrap-foot')]
        rows = table.findall('.//tbody/tr') or table.findall('.//table/tr')
        for index, row in enumerate(rows, 1):
            cells = [{'text': normalize_text(' '.join(cell.itertext())), 'colspan': cell.get('colspan', '1'),
                      'rowspan': cell.get('rowspan', '1')} for cell in row if cell.tag in {'th', 'td'}]
            text = f'TABLE CELL SERIALIZATION [{identifier}, row {index}]. Caption: {caption}. '
            text += 'Header rows: ' + json.dumps(header_rows, ensure_ascii=False) + '. '
            text += 'Cells in source order: ' + json.dumps(cells, ensure_ascii=False) + '. '
            text += 'Footnotes: ' + ' '.join(notes)
            blocks.append({'text': text, 'source_kind': 'table_row', 'table_id': identifier,
                           'row_number': index, 'table_headers': header_rows, 'table_cells': cells,
                           'table_footnotes': notes, 'section_heading': caption,
                           'layout_verified': False, 'paragraph_references': []})
    for number, figure in enumerate(root.findall('.//fig'), 1):
        caption = figure.find('caption')
        if caption is not None:
            blocks.append({'text': normalize_text(' '.join(caption.itertext())), 'source_kind': 'figure_caption',
                           'figure_id': figure.get('id') or f'figure-{number}', 'layout_verified': False,
                           'section_heading': 'Figure caption', 'paragraph_references': []})
    return blocks


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def extract_paragraphs(xml_text: str, with_sections: bool = False) -> list[Any]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    paragraphs: list[Any] = []
    parents = {child: parent for parent in root.iter() for child in parent}
    bibliography = {}
    for reference in root.findall('.//ref-list/ref'):
        ref_id = reference.get('id')
        if ref_id:
            ids = reference.findall('.//pub-id')
            bibliography[ref_id] = {'reference_id': ref_id,
                'citation_text': normalize_text(' '.join(reference.itertext()))[:2000],
                'doi': next((normalize_text(' '.join(p.itertext())) for p in ids if p.get('pub-id-type') == 'doi'), '')}
    excluded_tags = {"ref-list", "ack", "fn-group", "table-wrap", "fig"}
    for element in root.findall(".//abstract//p") + root.findall(".//body//p"):
        ancestor = parents.get(element)
        excluded = False
        while ancestor is not None:
            tag = ancestor.tag.rsplit("}", 1)[-1]
            section_type = str(ancestor.attrib.get("sec-type") or "").casefold()
            if tag in excluded_tags or section_type in {"references", "acknowledgments", "funding", "conflict-of-interest"}:
                excluded = True
                break
            ancestor = parents.get(ancestor)
        if excluded:
            continue
        text = normalize_text(" ".join(element.itertext()))
        if len(text) >= 80:
            if with_sections:
                parent = parents.get(element)
                heading = ""
                while parent is not None:
                    if parent.tag.rsplit("}", 1)[-1] == "abstract":
                        heading = "Abstract"
                        break
                    title = parent.find("title")
                    if title is not None:
                        heading = normalize_text(" ".join(title.itertext()))
                        break
                    parent = parents.get(parent)
                links = [bibliography[rid] for xref in element.findall('.//xref')
                         if xref.get('ref-type') == 'bibr' for rid in str(xref.get('rid') or '').split()
                         if rid in bibliography]
                paragraphs.append({"text": text, "section_heading": heading, 'paragraph_references': links})
            else:
                paragraphs.append(text)
    blocks = _xml_table_and_figure_blocks(root)
    return paragraphs + (blocks if with_sections else [b['text'] for b in blocks])


def scholarly_sentences(text: str) -> list[str]:
    compact = normalize_text(text)
    protected = re.sub(r"\b(?:et\s+al|e\.g|i\.e|spp?|figs?|refs?|eqs?|vs)\.",
                       lambda match: match.group(0).replace(".", "<DOT>"), compact, flags=re.IGNORECASE)
    return [piece.replace("<DOT>", ".").strip() for piece in
            re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(\[])", protected) if piece.strip()]


def make_chunks(paragraphs: list[Any], size: int = 280, overlap: int = 50) -> list[Any]:
    chunks: list[Any] = []
    for paragraph in paragraphs:
        record = paragraph if isinstance(paragraph, dict) else {"text": str(paragraph)}
        words = str(record.get("text") or "").split()
        if len(words) <= size:
            chunks.append({**record, "text": " ".join(words)} if isinstance(paragraph, dict) else " ".join(words))
            continue
        sentences = scholarly_sentences(str(record.get("text") or ""))
        start = 0
        while start < len(sentences):
            end, count = start, 0
            while end < len(sentences) and (end == start or count + len(sentences[end].split()) <= size):
                count += len(sentences[end].split())
                end += 1
            text = " ".join(sentences[start:end])
            chunks.append({**record, "text": text} if isinstance(paragraph, dict) else text)
            if end == len(sentences):
                break
            next_start, overlap_count = end, 0
            while next_start > start + 1 and overlap_count < overlap:
                next_start -= 1
                overlap_count += len(sentences[next_start].split())
            start = next_start
    return chunks


def article_metadata(result: dict[str, Any], article_key: str) -> dict[str, Any]:
    source = str(result.get("source") or "").strip()
    source_id = str(result.get("id") or "").strip()
    europe_pmc_url = (
        f"https://europepmc.org/article/{urllib.parse.quote(source, safe='')}/{urllib.parse.quote(source_id, safe='')}"
        if source and source_id and source.upper() != "OPENALEX" else ""
    )
    return {"evidence_document_id": article_key, "source": result.get("source"), "source_id": result.get("id"),
        "pmid": result.get("pmid"), "pmcid": result.get("pmcid"), "doi": result.get("doi"),
        "title": result.get("title"), "journal": result.get("journalTitle"), "publication_year": result.get("pubYear"),
        "author_string": result.get("authorString"), "is_open_access": is_open_access(result),
        "source_url": result.get("_landing_page_url") or europe_pmc_url or result.get("_open_access_pdf_url"),
        "open_access_license": result.get("_open_access_license"),
        "publication_types": (result.get("pubTypeList") or {}).get("pubType", [])}
