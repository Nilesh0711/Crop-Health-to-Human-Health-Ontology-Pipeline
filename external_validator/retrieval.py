"""Europe PMC, Crossref, and OpenAlex access; external full text stays in memory."""

from __future__ import annotations

import json
import io
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import time
from typing import Any

from pypdf import PdfReader

from .config import Settings


SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
FULLTEXT_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{source}/{article_id}/fullTextXML"
FULLTEXT_BY_ID_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{article_id}/fullTextXML"
CROSSREF_URL = "https://api.crossref.org/works"
OPENALEX_WORK_URL = "https://api.openalex.org/works/{doi_url}"
OPENALEX_SEARCH_URL = "https://api.openalex.org/works"
_REFERENCE_STOPWORDS = {"a", "an", "and", "the", "of", "for", "in", "on", "to", "with", "from", "by"}


def fetch_json(url: str, settings: Settings) -> dict[str, Any]:
    """Read scholarly metadata with a bounded retry for transient APIs.

    This retries availability and rate-limit failures only. It never repeats a
    model verdict, changes a query, or turns absent evidence into support.
    """
    request = urllib.request.Request(url, headers={"User-Agent": settings.user_agent, "Accept": "application/json"})
    for attempt in range(2):
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                return json.loads(response.read(settings.max_fulltext_bytes).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code not in {429, 500, 502, 503, 504} or attempt:
                raise
            time.sleep(0.6)
    raise RuntimeError("unreachable metadata retrieval state")


def search(query: str, settings: Settings, page_size: int) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"query": query, "format": "json", "pageSize": page_size, "resultType": "core"})
    return fetch_json(f"{SEARCH_URL}?{params}", settings).get("resultList", {}).get("result", [])


def resolve_bibliographic_reference(reference: dict[str, Any], settings: Settings) -> dict[str, Any] | None:
    """Resolve a source-paper bibliography entry without treating it as evidence."""
    citation = str(reference.get("citation_text") or "").strip()
    known_doi = str(reference.get("doi") or "").strip()
    if known_doi:
        return {"doi": known_doi, "title": "", "reference_number": reference.get("reference_number")}
    if not citation:
        return None
    params = urllib.parse.urlencode({"query.bibliographic": citation[:1500], "rows": 1,
                                     "select": "DOI,title,published-print,published-online,container-title"})
    try:
        items = fetch_json(f"{CROSSREF_URL}?{params}", settings).get("message", {}).get("items", [])
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        return None
    if not items:
        return None
    item = items[0]
    title_value = item.get("title") or []
    title = str(title_value[0] if isinstance(title_value, list) and title_value else title_value or "").strip()
    title_terms = {term for term in re.findall(r"[a-z0-9]+", title.casefold()) if term not in _REFERENCE_STOPWORDS}
    citation_terms = set(re.findall(r"[a-z0-9]+", citation.casefold()))
    # Crossref's top result is only retained when its title is visibly present
    # in the local bibliography entry. This prevents a fuzzy resolver result
    # from silently becoming a cited evidence paper.
    if len(title_terms) < 3 or len(title_terms & citation_terms) / len(title_terms) < 0.65:
        return None
    return {"doi": str(item.get("DOI") or "").strip().casefold(), "title": title,
            "reference_number": reference.get("reference_number")}


def openalex_open_access_copy(doi: str, settings: Settings) -> dict[str, Any] | None:
    """Return usable OpenAlex evidence metadata for a cited DOI.

    A public PDF is preferred, but an indexed abstract is also usable evidence.
    Both still pass passage reranking and entailment; the bibliography/metadata
    record alone never counts as support. Public PDF bytes remain transient.
    """
    normalized_doi = str(doi or "").strip().casefold()
    if not normalized_doi:
        return None
    doi_url = urllib.parse.quote(f"https://doi.org/{normalized_doi}", safe="")
    try:
        work = fetch_json(OPENALEX_WORK_URL.format(doi_url=doi_url), settings)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        return None
    location = work.get("best_oa_location") or work.get("primary_location") or {}
    pdf_url = str(location.get("pdf_url") or "").strip()
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
    exact Qwen/anchor judgement used for every external document.
    """
    # Broad records without an abstract or public PDF cannot be judged and
    # only crowd out usable studies, so require indexed abstract text here.
    params = urllib.parse.urlencode({
        "search": query,
        "filter": "has_abstract:true",
        "select": ("id,doi,title,publication_year,authorships,abstract_inverted_index,"
                   "open_access,best_oa_location,primary_location"),
        "per-page": min(max(1, page_size), 200),
    })
    payload = fetch_json(f"{OPENALEX_SEARCH_URL}?{params}", settings)
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
        pdf_url = str(location.get("pdf_url") or "").strip()
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
    if not str(url or "").startswith(("https://", "http://")):
        return []
    request = urllib.request.Request(str(url), headers={
        "User-Agent": settings.user_agent,
        "Accept": "application/pdf",
    })
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            payload = response.read(settings.max_fulltext_bytes + 1)
            content_type = str(response.headers.get("Content-Type") or "").casefold()
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError):
        return []
    if len(payload) > settings.max_fulltext_bytes or ("pdf" not in content_type and not payload.startswith(b"%PDF")):
        return []
    try:
        reader = PdfReader(io.BytesIO(payload))
        pages: list[dict[str, Any]] = []
        references_started = False
        for page_number, page in enumerate(reader.pages[:80], 1):
            raw = page.extract_text() or ""
            lines = [line.strip() for line in raw.splitlines() if line.strip()]
            kept: list[str] = []
            for line in lines:
                if re.match(r"^(?:\d+(?:\.\d+)*)?\s*(?:references|bibliography)\s*$", line, re.IGNORECASE):
                    references_started = True
                    break
                if re.match(r"^(?:acknowledg(?:e)?ments?|author contributions|funding|conflicts? of interest)\s*$",
                            line, re.IGNORECASE):
                    break
                if re.match(r"^(?:figure|table)\s+\d+\s*[.:]", line, re.IGNORECASE):
                    continue
                kept.append(line)
            if references_started:
                break
            text = normalize_text(" ".join(kept))
            if len(text) >= 80:
                pages.append({"page_number": page_number, "text": text})
    except Exception:
        return []
    return pages


def fetch_open_access_pdf_text(url: str, settings: Settings) -> str | None:
    """Backward-compatible text view; the workflow uses page-aware records."""
    pages = fetch_open_access_pdf_pages(url, settings)
    return "\n\n".join(page["text"] for page in pages) or None


def paragraphs_from_plain_text(text: str) -> list[str]:
    """Turn transient PDF text into conservative paragraph candidates."""
    pieces = [normalize_text(piece) for piece in re.split(r"\n{2,}|(?<=[.!?])\s{2,}", str(text or ""))]
    return [piece for piece in pieces if len(piece) >= 80]


def paragraphs_from_pdf_pages(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Create page-aware paragraph records after bibliography/caption removal."""
    paragraphs: list[dict[str, Any]] = []
    for page in pages:
        for text in paragraphs_from_plain_text(str(page.get("text") or "")):
            if re.match(r"^(?:figure|table)\s+\d+\s*[.:]", text, re.IGNORECASE):
                continue
            paragraphs.append({"page_number": page.get("page_number"), "text": text})
    return paragraphs


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def extract_paragraphs(xml_text: str) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    paragraphs: list[str] = []
    parents = {child: parent for parent in root.iter() for child in parent}
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
            paragraphs.append(text)
    return paragraphs


def make_chunks(paragraphs: list[Any], size: int = 280, overlap: int = 50) -> list[Any]:
    chunks: list[Any] = []
    for paragraph in paragraphs:
        record = paragraph if isinstance(paragraph, dict) else {"text": str(paragraph)}
        words = str(record.get("text") or "").split()
        if len(words) <= size:
            chunks.append({**record, "text": " ".join(words)} if isinstance(paragraph, dict) else " ".join(words))
            continue
        for start in range(0, len(words), max(1, size - overlap)):
            chunk = words[start:start + size]
            if len(chunk) < 40:
                break
            text = " ".join(chunk)
            chunks.append({**record, "text": text} if isinstance(paragraph, dict) else text)
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
