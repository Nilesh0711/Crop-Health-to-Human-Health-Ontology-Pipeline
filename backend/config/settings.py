"""Runtime configuration for the active neighboring-claim pipeline."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from backend.config.paths import PROJECT_ROOT, ONTOLOGY_DIR, PDF_DIR, CLI_OUTPUT_DIR

# Preserve extraction compatibility; source-scope and pathway policies version separately.
SCHEMA_VERSION = "external-validation-v36"
APPLICATION_VERSION = "neighbor-validation-v50"

DEFAULT_EXTRACTION_MODEL = "opencode-go/deepseek-v4.1-flash"
DEFAULT_SOURCE_MODEL = "opencode-go/deepseek-v4.1-flash"
DEFAULT_QUERY_MODEL = "opencode-go/mimo-v2.6-flash"
DEFAULT_EXTERNAL_MODEL = "opencode-go/muse-spark-1.3-contributor"

GO_PIPELINE_MODELS = tuple(f'opencode-go/{name}' for name in (
    'deepseek-v4.1-flash', 'mimo-v2.6-flash', 'glm-5.3-flash', 'mimo-v2.6-pro',
    'muse-spark-1.3-contributor',
))

ALLOWED_PIPELINE_MODELS = GO_PIPELINE_MODELS

TASK_DEFAULT_MODELS = {
    'extraction': DEFAULT_EXTRACTION_MODEL,
    'source': DEFAULT_SOURCE_MODEL,
    'query': DEFAULT_QUERY_MODEL,
    'external': DEFAULT_EXTERNAL_MODEL,
    'pathway_assembly': 'opencode-go/mimo-v2.6-flash',
}


def _default_neural_device() -> str:
    try:
        import torch
        if torch.cuda.is_available():
            return 'cuda'
    except Exception:
        pass
    return 'cpu'


@dataclass(frozen=True)
class Settings:
    """Runtime configuration for hosted LLMs and local specialist models."""

    thinking: dict[str, bool] = field(default_factory=lambda: {task: False for task in TASK_DEFAULT_MODELS})
    extraction_model: str = os.getenv("EXTRACTION_MODEL", DEFAULT_EXTRACTION_MODEL)
    source_model: str = os.getenv('OPEN_CODE_SOURCE_MODEL', TASK_DEFAULT_MODELS['source'])
    query_model: str = os.getenv('OPEN_CODE_QUERY_MODEL', TASK_DEFAULT_MODELS['query'])
    external_model: str = os.getenv('OPEN_CODE_EXTERNAL_MODEL', DEFAULT_EXTERNAL_MODEL)
    external_paper_model: str = os.getenv('OPEN_CODE_EXTERNAL_PAPER_MODEL', DEFAULT_SOURCE_MODEL)
    pathway_assembly_model: str = os.getenv('OPEN_CODE_PATHWAY_ASSEMBLY_MODEL', 'opencode-go/mimo-v2.6-flash')
    pathway_assembly_batch_size: int = int(os.getenv('PATHWAY_ASSEMBLY_BATCH_SIZE', '24'))
    pathway_assembly_input_characters: int = int(os.getenv('PATHWAY_ASSEMBLY_INPUT_CHARACTERS', '140000'))
    pathway_candidate_strategy: str = os.getenv('PATHWAY_CANDIDATE_STRATEGY', 'indexed')
    pathway_candidate_top_k: int = int(os.getenv('PATHWAY_CANDIDATE_TOP_K', '10'))
    pathway_candidate_expansion_k: int = int(os.getenv('PATHWAY_CANDIDATE_EXPANSION_K', '30'))
    pathway_candidate_small_registry: int = int(os.getenv('PATHWAY_CANDIDATE_SMALL_REGISTRY', '64'))
    external_top_k: int = int(os.getenv('EXTERNAL_TOP_K', '8'))
    external_top_chunks: int = int(os.getenv('EXTERNAL_TOP_CHUNKS', '4'))
    neighbor_max_pairs: int = int(os.getenv('NEIGHBOR_MAX_PAIRS', '100'))
    neighbor_workers: int = int(os.getenv('NEIGHBOR_WORKERS', '2'))
    source_workers: int = int(os.getenv('SOURCE_WORKERS', '2'))
    pathway_followup_papers: int = int(os.getenv('PATHWAY_FOLLOWUP_PAPERS', '3'))
    external_paper_workers: int = int(os.getenv('EXTERNAL_PAPER_WORKERS', '3'))
    evidence_index_enabled: bool = os.getenv('EVIDENCE_INDEX_ENABLED', '1') != '0'
    evidence_index_max_mb: int = int(os.getenv('EVIDENCE_INDEX_MAX_MB', '512'))
    evidence_index_ttl_hours: int = int(os.getenv('EVIDENCE_INDEX_TTL_HOURS', '168'))
    search_wait_seconds: float = float(os.getenv('SEARCH_WAIT_SECONDS', '60'))
    metadata_timeout_seconds: float = float(os.getenv('METADATA_TIMEOUT_SECONDS', '45'))
    external_input_max_characters: int = int(os.getenv('EXTERNAL_INPUT_MAX_CHARACTERS', '240000'))
    opencode_response_format: str = os.getenv('OPENCODE_RESPONSE_FORMAT', 'model_default')
    json_repair_attempts: int = int(os.getenv('JSON_REPAIR_ATTEMPTS', '1'))
    json_regeneration_attempts: int = int(os.getenv('JSON_REGENERATION_ATTEMPTS', '1'))
    regeneration_max_output_tokens: int = int(os.getenv('REGENERATION_MAX_OUTPUT_TOKENS', '32768'))
    embedding_model: str = os.getenv("EMBEDDING_MODEL", "BAAI/bge-base-en-v1.5")
    embedding_device: str = os.getenv("EMBEDDING_DEVICE", _default_neural_device())
    embedding_query_instruction: str = os.getenv(
        "EMBEDDING_QUERY_INSTRUCTION",
        "Represent this sentence for searching relevant passages: ",
    )
    cross_encoder_model: str = os.getenv("CROSS_ENCODER_MODEL", "ncbi/MedCPT-Cross-Encoder")
    cross_encoder_device: str = os.getenv("CROSS_ENCODER_DEVICE", _default_neural_device())
    cross_encoder_candidates: int = int(os.getenv("CROSS_ENCODER_CANDIDATES", "20"))
    extraction_claims_per_packet: int = int(os.getenv("EXTRACTION_CLAIMS_PER_PACKET", "0"))
    extraction_concurrency: int = int(os.getenv("EXTRACTION_CONCURRENCY", "4"))
    extraction_mode: str = os.getenv('EXTRACTION_MODE', 'target_context')
    extraction_target_words: int = int(os.getenv('EXTRACTION_TARGET_WORDS', '800'))
    fallback_models: dict[str, str] = field(default_factory=lambda: {
        'extraction': 'opencode-go/mimo-v2.6-flash',
        'source': 'opencode-go/mimo-v2.6-flash',
        'pathway_assembly': 'opencode-go/deepseek-v4.1-flash',
    })
    request_timeout_seconds: int = int(os.getenv("VALIDATION_TIMEOUT_SECONDS", "300"))
    external_timeout_seconds: int = int(os.getenv("EXTERNAL_TIMEOUT_SECONDS", "1200"))
    extraction_timeout_seconds: int = int(os.getenv("EXTRACTION_TIMEOUT_SECONDS", "420"))
    extraction_min_output_budget: int = int(os.getenv('EXTRACTION_MIN_OUTPUT_BUDGET', '24576'))
    external_min_output_budget: int = int(os.getenv('EXTERNAL_MIN_OUTPUT_BUDGET', '28192'))
    validation_batch_size: int = int(os.getenv("VALIDATION_BATCH_SIZE", "3"))
    embedding_batch_size: int = int(os.getenv('EMBEDDING_BATCH_SIZE', '16'))
    cross_encoder_batch_size: int = int(os.getenv('CROSS_ENCODER_BATCH_SIZE', '8'))
    retrieval_workers: int = int(os.getenv('RETRIEVAL_WORKERS', '6'))
    openalex_api_key: str = os.getenv("OPENALEX_API_KEY", "AG6vUPB0KN2xogepXwWeLI")
    openalex_email: str = os.getenv("OPENALEX_EMAIL", "nilesh.cs25@kgpian.iitkgp.ac.in") or os.getenv("OPENALEX_MAILTO", "")
    search_cache_ttl_hours: int = int(os.getenv("SEARCH_CACHE_TTL_HOURS", "168"))
    max_fulltext_bytes: int = 8 * 1024 * 1024
    user_agent: str = "crop-to-human-health-ontology/0.2 (external evidence validation)"

    def __post_init__(self) -> None:
        if not 1 <= self.neighbor_workers <= 2 or not 1 <= self.source_workers <= 4:
            raise ValueError('Neighbor workers must be 1..2 and source workers 1..4')
        if self.pathway_candidate_strategy not in {'indexed', 'exhaustive'}:
            raise ValueError('Pathway candidate strategy must be indexed or exhaustive')
        if not 1 <= self.pathway_candidate_top_k <= self.pathway_candidate_expansion_k <= 500:
            raise ValueError('Pathway candidate budgets require 1 <= top-k <= expansion-k <= 500')
        if not 0 <= self.pathway_candidate_small_registry <= 1000:
            raise ValueError('Small registry threshold must be 0..1000')
        if not 1 <= self.external_paper_workers <= 4:
            raise ValueError('External paper workers must be 1..4')
        if self.evidence_index_max_mb < 1 or self.evidence_index_ttl_hours < 1:
            raise ValueError('Evidence index size and refresh period must be positive')
        if self.search_wait_seconds <= 0 or self.metadata_timeout_seconds <= 0:
            raise ValueError('Search deadlines must be positive')
        if not 1 <= self.neighbor_max_pairs <= 500 or not 0 <= self.pathway_followup_papers <= 10:
            raise ValueError('Neighbor budgets require 1..500 pairs and 0..10 follow-up papers')
        if not isinstance(self.thinking, dict) or set(self.thinking) - TASK_DEFAULT_MODELS.keys() or any(type(v) is not bool for v in self.thinking.values()):
            raise ValueError('Thinking settings require known stage names and boolean values')
        if self.extraction_mode not in {'target_context', 'whole_packet'}:
            raise ValueError('EXTRACTION_MODE must be target_context or whole_packet')
        if not 200 <= self.extraction_target_words <= 2000:
            raise ValueError('EXTRACTION_TARGET_WORDS must be between 200 and 2000')
        if not 1 <= self.extraction_concurrency <= 8:
            raise ValueError('EXTRACTION_CONCURRENCY must be between 1 and 8')
        for task, fallback_selector in self.fallback_models.items():
            if fallback_selector not in ALLOWED_PIPELINE_MODELS:
                raise ValueError(f'Fallback model {fallback_selector} for {task} must use an approved OpenCode Go model')
        if self.opencode_response_format not in {'model_default', 'json_object', 'json_schema'}:
            raise ValueError('OPENCODE_RESPONSE_FORMAT must be model_default, json_object or json_schema')
        if self.json_repair_attempts not in {0, 1}:
            raise ValueError('JSON_REPAIR_ATTEMPTS must be 0 or 1')
        if self.json_regeneration_attempts not in {0, 1}:
            raise ValueError('JSON_REGENERATION_ATTEMPTS must be 0 or 1')
        if self.regeneration_max_output_tokens < 8192:
            raise ValueError('REGENERATION_MAX_OUTPUT_TOKENS must be at least 8192')
        for task, selector in {**self.task_models, 'external_paper': self.external_paper_model}.items():
            if selector not in ALLOWED_PIPELINE_MODELS:
                raise ValueError(f'{task} must use an approved OpenCode Go model')
        if not 1 <= self.external_top_k <= 30 or not 1 <= self.external_top_chunks <= 12:
            raise ValueError('External top-k must be 1..30 and chunks per paper 1..12')
        if self.external_input_max_characters < 1000:
            raise ValueError('External context character budget must be at least 1000')
        if min(self.request_timeout_seconds, self.extraction_timeout_seconds, self.external_timeout_seconds) < 1:
            raise ValueError("Model request timeouts must be positive")
        if self.extraction_min_output_budget < 1:
            raise ValueError('EXTRACTION_MIN_OUTPUT_BUDGET must be positive')
        if self.external_min_output_budget < 8192:
            raise ValueError('EXTERNAL_MIN_OUTPUT_BUDGET must be at least 8192')
        if not 0 <= self.extraction_claims_per_packet <= 8:
            raise ValueError("EXTRACTION_CLAIMS_PER_PACKET must be between 0 and 8; 0 means all eligible claims")
        if not 1 <= self.pathway_assembly_batch_size <= 64 or self.pathway_assembly_input_characters < 1000:
            raise ValueError('Pathway assembly batch size must be 1..64 and input budget at least 1000 characters')
        if not 1 <= self.embedding_batch_size <= 128 or not 1 <= self.cross_encoder_batch_size <= 128:
            raise ValueError('Embedding and cross-encoder batch sizes must be 1..128')
        if self.cross_encoder_candidates < 1:
            raise ValueError("CROSS_ENCODER_CANDIDATES must be positive")
        for name, value in (("VALIDATION_BATCH_SIZE", self.validation_batch_size),
                            ("RETRIEVAL_WORKERS", self.retrieval_workers)):
            if not 1 <= value <= 8:
                raise ValueError(f"{name} must be between 1 and 8")

    @property
    def knowledge_building_mode(self) -> str:
        """Saved-run metadata label; executable legacy modes are research-only."""
        return 'pathway_only'

    @property
    def external_judgement_mode(self) -> str:
        return 'pathway_only'

    @property
    def task_thinking(self) -> dict[str, bool]:
        return {task: self.thinking.get(task, False) for task in TASK_DEFAULT_MODELS}

    @property
    def task_models(self) -> dict[str, str]:
        return {task: getattr(self, f'{task}_model') for task in TASK_DEFAULT_MODELS}

    @property
    def external_phase_models(self) -> dict[str, str]:
        return {'paper': self.external_paper_model, 'synthesis': self.external_model}
