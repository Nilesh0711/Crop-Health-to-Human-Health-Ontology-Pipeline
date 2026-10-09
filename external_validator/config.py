"""Central configuration for the external validation pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ONTOLOGY_DIR = PROJECT_ROOT / "ontology"
PDF_DIR = PROJECT_ROOT / "pdf"
RAG_DIR = PROJECT_ROOT / "rag"
SCHEMA_VERSION = "external-validation-v8"
# qwen3:8b fully fits on the workstation's 8 GiB GPU far more readily than
# the 10 GiB Qwen 14B Q4 model.  It is the local-first default; individual
# runs can still select another installed model in the UI.
DEFAULT_MODEL = "qwen3:8b"
BLOCKED_PIPELINE_MODELS = frozenset()


@dataclass(frozen=True)
class Settings:
    """Runtime configuration. Environment variables keep local endpoints configurable."""

    ollama_url: str = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
    model: str = os.getenv("VALIDATION_MODEL", DEFAULT_MODEL)
    context_tokens: int = int(os.getenv("OLLAMA_CONTEXT_TOKENS", "8192"))
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "BAAI/bge-small-en-v1.5")
    request_timeout_seconds: int = int(os.getenv("VALIDATION_TIMEOUT_SECONDS", "300"))
    max_fulltext_bytes: int = 8 * 1024 * 1024
    user_agent: str = "crop-to-human-health-ontology/0.2 (external evidence validation)"

    def __post_init__(self) -> None:
        if self.model in BLOCKED_PIPELINE_MODELS:
            raise ValueError(f"{self.model} is disabled for this pipeline; use {DEFAULT_MODEL} or another installed model")
