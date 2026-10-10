"""Central configuration for the external validation pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
ONTOLOGY_DIR = PROJECT_ROOT / "ontology"
PDF_DIR = PROJECT_ROOT / "pdf"
RAG_DIR = PROJECT_ROOT / "rag"
# v12 adds claim-level source spans, conservative vocabulary normalization,
# independent-evidence accounting, two-stage reranking, and an NLI veto.
# Keeping this distinct prevents new results from being confused with older
# validation records that used different acceptance semantics.
SCHEMA_VERSION = "external-validation-v12"
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
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "BAAI/bge-large-en-v1.5")
    embedding_device: str = os.getenv("EMBEDDING_DEVICE", "cpu")
    embedding_query_instruction: str = os.getenv(
        "EMBEDDING_QUERY_INSTRUCTION",
        "Represent this sentence for searching relevant passages: ",
    )
    cross_encoder_model: str = os.getenv("CROSS_ENCODER_MODEL", "ncbi/MedCPT-Cross-Encoder")
    cross_encoder_device: str = os.getenv("CROSS_ENCODER_DEVICE", "cpu")
    cross_encoder_candidates: int = int(os.getenv("CROSS_ENCODER_CANDIDATES", "20"))
    nli_model: str = os.getenv("NLI_MODEL", "cross-encoder/nli-deberta-v3-large")
    nli_device: str = os.getenv("NLI_DEVICE", "cpu")
    enable_nli: bool = os.getenv("ENABLE_NLI_VALIDATOR", "1").strip().lower() not in {"0", "false", "no"}
    extraction_claims_per_packet: int = int(os.getenv("EXTRACTION_CLAIMS_PER_PACKET", "4"))
    request_timeout_seconds: int = int(os.getenv("VALIDATION_TIMEOUT_SECONDS", "300"))
    max_fulltext_bytes: int = 8 * 1024 * 1024
    user_agent: str = "crop-to-human-health-ontology/0.2 (external evidence validation)"

    def __post_init__(self) -> None:
        if self.model in BLOCKED_PIPELINE_MODELS:
            raise ValueError(f"{self.model} is disabled for this pipeline; use {DEFAULT_MODEL} or another installed model")
        if not 1 <= self.extraction_claims_per_packet <= 8:
            raise ValueError("EXTRACTION_CLAIMS_PER_PACKET must be between 1 and 8")
        if self.cross_encoder_candidates < 1:
            raise ValueError("CROSS_ENCODER_CANDIDATES must be positive")
