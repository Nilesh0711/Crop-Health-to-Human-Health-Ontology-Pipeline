"""Literal normalization and surface-span matching shared by parsing and extraction."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass


SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class SurfaceToken:
    text: str
    start: int
    end: int


def source_match_tokens(source, wanted=()):
    """A matching-only view; offsets still address the untouched source text.

    Join a spaced/line-wrapped hyphen only when the requested word supplies
    exactly the same letters. Ordinary compounds and numerical signs stay raw.
    """
    tokens = [SurfaceToken(unicodedata.normalize('NFKC', m[0]).casefold(), m.start(), m.end())
              for m in re.finditer(r'\w+', source)]
    result, index, wanted = [], 0, set(wanted)
    while index < len(tokens):
        left = tokens[index]
        if index + 1 < len(tokens):
            right = tokens[index + 1]
            if (len(left.text) >= 2 and len(right.text) >= 2 and left.text.isalpha() and right.text.isalpha()
                    and left.text + right.text in wanted
                    and re.fullmatch(r'(?:\s+-\s+|-\s*[\r\n]+\s*)', source[left.end:right.start])):
                result.append(SurfaceToken(left.text + right.text, left.start, right.end))
                index += 2
                continue
        result.append(left)
        index += 1
    return result


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


def head_token_matches(w_source: str, w_wanted: str) -> bool:
    """Check exact match or conservative regular English singular/plural head-noun match."""
    if w_source == w_wanted:
        return True
    if len(w_source) >= 4 and len(w_wanted) >= 4:
        if w_source == w_wanted + "s" or w_wanted == w_source + "s":
            return True
        if w_source == w_wanted + "es" or w_wanted == w_source + "es":
            return True
        if (w_source.endswith("ies") and w_source[:-3] + "y" == w_wanted) or (w_wanted.endswith("ies") and w_wanted[:-3] + "y" == w_source):
            return True
    return False


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
    source = str(source_text or "")
    wanted = [word.casefold() for word in mention_words]
    for requested in ((), wanted):
        tokens = source_match_tokens(source, requested)
        available = [token.text for token in tokens]
        for index in range(len(available) - len(wanted) + 1):
            if available[index:index + len(wanted)] == wanted:
                return source[tokens[index].start:tokens[index + len(wanted) - 1].end]
        for index in range(len(available) - len(wanted) + 1):
            sub = available[index:index + len(wanted)]
            if sub[:-1] == wanted[:-1] and head_token_matches(sub[-1], wanted[-1]):
                return source[tokens[index].start:tokens[index + len(wanted) - 1].end]
    return None
