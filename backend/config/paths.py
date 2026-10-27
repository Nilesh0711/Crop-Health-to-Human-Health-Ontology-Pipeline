"""Repository paths, independent of the shell's current working directory.

Keep PDF/run locations stable: saved provenance and existing tools use them.
Application code imports paths here instead of reconstructing resource paths.
"""
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
PDF_DIR = PROJECT_ROOT / 'pdf'
ONTOLOGY_DIR = PROJECT_ROOT / 'ontology'
REPORTS_DIR = PROJECT_ROOT / 'reports'
CLI_OUTPUT_DIR = REPORTS_DIR / 'external_validation'
BACKEND_DIR = PROJECT_ROOT / 'backend'
EXTRACTION_PROMPT = BACKEND_DIR / 'stages' / 'claim_extraction' / 'prompt.md'
EXTERNAL_VALIDATION_PROMPT = BACKEND_DIR / 'stages' / 'external_validation' / 'prompt_reference.md'
FRONTEND_DIST_DIR = PROJECT_ROOT / 'frontend' / 'dist'
