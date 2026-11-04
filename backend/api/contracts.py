"""Validated request contracts for the workbench API."""

from typing import Literal
from pydantic import BaseModel, Field, StrictBool, ConfigDict, AliasChoices
from backend.config.settings import Settings


class ThinkingOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    extraction: StrictBool = False
    source: StrictBool = False
    query: StrictBool = False
    external: StrictBool = False
    pathway_assembly: StrictBool = False

class RunOptions(BaseModel):
    neighbor_max_pairs: int = Field(default_factory=lambda: Settings().neighbor_max_pairs, ge=1, le=500)
    thinking: ThinkingOptions = Field(default_factory=ThinkingOptions)
    max_pages: int = Field(default=0, ge=0, le=1000, description="0 means all text pages")
    articles_per_pathway: int = Field(default_factory=lambda: Settings().external_top_k, ge=1, le=30,
                                    validation_alias=AliasChoices("articles_per_pathway", "articles_per_claim"),
                                    description="Search papers per neighboring pair; usable attached citations are additional")
    claims_per_packet: int = Field(default_factory=lambda: Settings().extraction_claims_per_packet, ge=0, le=8)
    extraction_mode: Literal['target_context', 'whole_packet'] = Field(
        default_factory=lambda: Settings().extraction_mode)
    extraction_target_words: int = Field(default_factory=lambda: Settings().extraction_target_words, ge=200, le=2000)
    extraction_generation_budget: int = Field(
        default_factory=lambda: Settings().extraction_min_output_budget, ge=8192, le=32768,
        description='Initial generation allowance shared by reasoning and the final answer')

class SourceReviewOptions(BaseModel):
    model_config = ConfigDict(extra='forbid')
    dimension: Literal['source_fidelity', 'project_scope']
    decision: Literal['accept', 'reject']
    expected_revision: str = Field(min_length=64, max_length=64)
    note: str = Field(default='', max_length=2000)
