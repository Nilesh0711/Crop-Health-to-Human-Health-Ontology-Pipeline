"""External, evidence-first validation for ontology candidates."""

from .candidates import build_candidates, parse_tuple_line
from .decision import decision_errors
from .retrieval import extract_paragraphs

__all__ = ["build_candidates", "decision_errors", "extract_paragraphs", "parse_tuple_line"]
