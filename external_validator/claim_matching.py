"""Conservative lexical anchors shared by source and external evidence checks."""

from __future__ import annotations

import re


_STOPWORDS = {"a", "an", "and", "at", "by", "for", "from", "in", "of", "on", "or", "the", "to", "with",
              "practice", "practices", "within", "concentration", "concentrations", "content", "contents",
              "level", "levels", "amount", "amounts"}


def _stem(token: str) -> str:
    if token.startswith("terrac"):
        return "terrac"
    if len(token) > 6 and token.endswith("ing"):
        return token[:-3]
    if len(token) > 5 and token.endswith("ies"):
        return token[:-3] + "y"
    if len(token) > 5 and token.endswith(("xes", "ches", "shes")):
        return token[:-2]
    if len(token) > 4 and token.endswith("s"):
        return token[:-1]
    return token


def entity_terms(value: str) -> set[str]:
    return {_stem(token) for token in re.findall(r"[a-z0-9]+", value.casefold()) if token not in _STOPWORDS}


def entity_anchored(value: str, evidence: str) -> bool:
    """Require an ordered, adjacent entity phrase in one explicit alternative.

    This deliberately sends synonyms and ambiguous paraphrases to review.
    """
    found = [_stem(token) for token in re.findall(r"[a-z0-9]+", evidence.casefold()) if token not in _STOPWORDS]
    alternatives = re.split(r"\s+or\s+|\s*/\s*", value.casefold())
    for alternative in alternatives:
        terms = [_stem(token) for token in re.findall(r"[a-z0-9]+", alternative) if token not in _STOPWORDS]
        if terms and any(found[index:index + len(terms)] == terms for index in range(len(found) - len(terms) + 1)):
            return True
    return False
