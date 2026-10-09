"""Europe PMC access. External full text is held in memory only."""

from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from typing import Any

from .config import Settings


SEARCH_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/search"
FULLTEXT_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{source}/{article_id}/fullTextXML"
FULLTEXT_BY_ID_URL = "https://www.ebi.ac.uk/europepmc/webservices/rest/{article_id}/fullTextXML"


def fetch_json(url: str, settings: Settings) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"User-Agent": settings.user_agent, "Accept": "application/json"})
    with urllib.request.urlopen(request, timeout=45) as response:
        return json.loads(response.read(settings.max_fulltext_bytes).decode("utf-8"))


def search(query: str, settings: Settings, page_size: int) -> list[dict[str, Any]]:
    params = urllib.parse.urlencode({"query": query, "format": "json", "pageSize": page_size, "resultType": "core"})
    return fetch_json(f"{SEARCH_URL}?{params}", settings).get("resultList", {}).get("result", [])


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


def normalize_text(value: str) -> str:
    return " ".join(value.split())


def extract_paragraphs(xml_text: str) -> list[str]:
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return []
    paragraphs: list[str] = []
    for element in root.findall(".//abstract//p") + root.findall(".//body//p"):
        text = normalize_text(" ".join(element.itertext()))
        if len(text) >= 80:
            paragraphs.append(text)
    return paragraphs


def make_chunks(paragraphs: list[str], size: int = 280, overlap: int = 50) -> list[str]:
    chunks: list[str] = []
    for paragraph in paragraphs:
        words = paragraph.split()
        if len(words) <= size:
            chunks.append(paragraph)
            continue
        for start in range(0, len(words), max(1, size - overlap)):
            chunk = words[start:start + size]
            if len(chunk) < 40:
                break
            chunks.append(" ".join(chunk))
    return chunks


def article_metadata(result: dict[str, Any], article_key: str) -> dict[str, Any]:
    return {"evidence_document_id": article_key, "source": result.get("source"), "source_id": result.get("id"),
        "pmid": result.get("pmid"), "pmcid": result.get("pmcid"), "doi": result.get("doi"),
        "title": result.get("title"), "journal": result.get("journalTitle"), "publication_year": result.get("pubYear"),
        "author_string": result.get("authorString"), "is_open_access": is_open_access(result)}
