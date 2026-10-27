"""Literal normalization and surface-span matching shared by parsing and extraction."""

from __future__ import annotations

import re
import unicodedata


SPACE_RE = re.compile(r"\s+")


def normalized(value: str) -> str:
    return SPACE_RE.sub(" ", value).strip().casefold()


def word_count(text: str) -> int:
    return len(re.findall(r"\w+", text))


def anchored_quote(source_text: str, proposed_quote: str) -> str | None:
    """Return the exact extracted-text span when all quote words occur in order."""
    words = re.findall(r"\w+", unicodedata.normalize("NFKC", proposed_quote))
    if len(words) < 5:
        return None
    pattern = r"(?<!\w)" + r"\W+".join(re.escape(word) for word in words) + r"(?!\w)"
    match = re.search(pattern, source_text, flags=re.IGNORECASE)
    return source_text[match.start():match.end()] if match else None


def source_surface_mention(source_text: str, proposed_mention: str) -> str | None:
    """Return a verbatim source mention when its words occur consecutively.

    Extraction is allowed to use a canonical entity name for graph merging, but
    it must also supply the exact surface mention it relied on.  This prevents
    an attractive paraphrase (or a CamelCase label) from becoming the only
    evidence anchor for a claim.
    """
    mention_words = re.findall(r"\w+", unicodedata.normalize("NFKC", str(proposed_mention or "")), flags=re.UNICODE)
    if not mention_words:
        return None
    source = unicodedata.normalize("NFKC", str(source_text or ""))
    tokens = list(re.finditer(r"\w+", source, flags=re.UNICODE))
    wanted = [word.casefold() for word in mention_words]
    available = [token.group(0).casefold() for token in tokens]
    for index in range(len(available) - len(wanted) + 1):
        if available[index:index + len(wanted)] == wanted:
            return source[tokens[index].start():tokens[index + len(wanted) - 1].end()]
    return None
