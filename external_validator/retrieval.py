"""Europe PMC access. External full text is held in memory only."""

from __future__ import annotations

import json
import io
import re
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from pypdf import PdfReader

from .config import Settings


SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
FULLTEXT_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{source}/{article_id}/fullTextXML"
FULLTEXT_BY_ID_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{article_id}/fullTextXML"
CROSSREF_URL = "https://api.crossref.org/works"
OPENALEX_WORK_URL = "https://api.openalex.org/works/{doi_url}"
_REFERENCE_STOPWORDS = {"a", "an", "and", "the", "of", "for", "in", "on", "to", "with", "from", "by"}


def fetch_json(url: str, settings: Settings) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": settings.user_agent, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.loads(response.read(settings.max_fulltext_bytes).decode("utf-8"))


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
    """Return a public PDF location for a cited DOI, if OpenAlex supplies one.

    This is deliberately a citation-only fallback, not a second broad search
    engine.  It improves coverage for agronomy references that are absent from
    Europe PMC while preserving the project's rule that a complete external
    paper is never written to disk.  A landing page is retained as metadata;
    only an explicit public PDF URL is fetched for transient evidence text.
    """
    normalized_doi = str(doi or "").strip().casefold()
    if not normalized_doi:
        return None
    doi_url = urllib.parse.quote(f"https://doi.org/{normalized_doi}", safe="")
    try:
        work = fetch_json(OPENALEX_WORK_URL.format(doi_url=doi_url), settings)
    except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, ValueError):
        return None
    if not bool((work.get("open_access") or {}).get("is_oa")):
        return None
    location = work.get("best_oa_location") or {}
    pdf_url = str(location.get("pdf_url") or "").strip()
    landing_page_url = str(location.get("landing_page_url") or "").strip()
    if not pdf_url:
        return None
    return {
        "source": "OPENALEX",
        "id": str(work.get("id") or normalized_doi),
        "doi": normalized_doi,
        "title": str(work.get("title") or "").strip(),
        "journalTitle": str((location.get("source") or {}).get("display_name") or "").strip(),
        "pubYear": str(work.get("publication_year") or "").strip(),
        "authorString": "",
        "abstractText": "",
        "isOpenAccess": "Y",
        "_open_access_pdf_url": pdf_url,
        "_landing_page_url": landing_page_url,
        "_open_access_license": str(location.get("license") or "").strip(),
    }


def is_open_access(result: dict[str, Any]) -> bool:
    return str(result.get("isOpenAccess", "")).upper() in {"Y", "YES", "TRUE", "1"}


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
