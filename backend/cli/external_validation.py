"""Commands for an OpenCode evidence-validation run with one primary model per task."""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

from backend.cli.candidates import build_candidates
from backend.config.settings import CLI_OUTPUT_DIR, Settings
from backend.shared.cache import RunCache
from backend.providers.llm import LLMServiceError, OpenCodeClient, format_request_metrics
from backend.providers.nli import NLIValidator
from backend.stages.evidence_ranking.passages import SemanticReranker
from backend.shared.storage import read_jsonl, write_jsonl
from backend.stages.external_validation.workflow import evidence_ledger, validate_candidate
from backend.stages.literature_retrieval.workflow import retrieve_only
from backend.stages.query_planning.planner import translate_queries


def _selected_candidates(args: argparse.Namespace) -> list[dict[str, Any]]:
    items = read_jsonl(CLI_OUTPUT_DIR / "candidates.jsonl")
    if args.candidate_id:
        items = [item for item in items if item["candidate_id"] == args.candidate_id]
    if args.limit is not None:
        items = items[:args.limit]
    if not items:
        raise SystemExit("No candidates selected. Run build-candidates first or choose a valid candidate ID.")
    return items


def command_build_candidates(args: argparse.Namespace) -> int:
    candidates, documents, errors = build_candidates(args.tuple_file)
    write_jsonl(CLI_OUTPUT_DIR / "candidates.jsonl", candidates)
    write_jsonl(CLI_OUTPUT_DIR / "documents.jsonl", documents)
    write_jsonl(CLI_OUTPUT_DIR / "candidate_parse_errors.jsonl", errors)
    print(f"Wrote {len(candidates)} candidates, {len(documents)} documents, and {len(errors)} parse errors.")
    return 0


def command_health_check(_: argparse.Namespace) -> int:
    settings = Settings()
    names = OpenCodeClient(settings).health_check().get("models", [])
    ready = {task: model.split('#', 1)[0] in names
             for task, model in settings.task_models.items()}
    if settings.external_judgement_mode == 'per_paper_synthesis':
        ready['external'] = ready['external'] and settings.external_paper_model in names
    print(json.dumps({"opencode_reachable": True, 'task_models': settings.task_models, 'task_ready': ready,
                      'external_phase_models': settings.external_phase_models,
                      'external_judgement_policy': settings.external_judgement_mode,
                      'external_top_k': settings.external_top_k,
                      'external_top_chunks': settings.external_top_chunks,
                      'model_policy': 'primary_only', "models": names}, indent=2))
    return 0 if all(ready.values()) else 2


def _run(args: argparse.Namespace, validate: bool) -> int:
    settings = Settings()
    client = OpenCodeClient(settings, settings.external_model, task='external',
                            on_request=lambda metrics: print(format_request_metrics(metrics), flush=True),
                            response_dir=CLI_OUTPUT_DIR / 'model_responses')
    reranker = SemanticReranker(settings)
    nli_validator = NLIValidator(settings) if validate and settings.enable_nli else None
    if nli_validator is not None:
        nli_validator.warmup()
    records: list[dict[str, Any]] = []
    candidates = _selected_candidates(args)
    helper = OpenCodeClient(settings, settings.query_model, task='query',
                             on_request=lambda metrics: print(format_request_metrics(metrics), flush=True),
                             response_dir=CLI_OUTPUT_DIR / 'model_responses')
    query_translations = translate_queries(candidates, helper, settings.query_batch_size, progress=print)
    cache = RunCache()
    for position, candidate in enumerate(candidates, 1):
        print(f"[{position}/{len(candidates)}] {candidate['raw_tuple']}")
        try:
            result = (validate_candidate(
                candidate, client, reranker, settings,
                args.articles_per_candidate, args.top_chunks,
                nli_validator=nli_validator,
                query_translation=query_translations[candidate["candidate_id"]], cache=cache,
            ) if validate else retrieve_only(
                candidate, client, reranker, settings,
                args.articles_per_candidate, args.top_chunks,
                query_translation=query_translations[candidate["candidate_id"]], cache=cache,
            ))
        except LLMServiceError:
            raise
        except Exception as exc:
            result = {"candidate_id": candidate["candidate_id"], "candidate": candidate, "validation_status": "pipeline_error", "error": str(exc)}
        records.append(result)
        if args.delay_seconds and position < len(candidates):
            time.sleep(args.delay_seconds)
    output = CLI_OUTPUT_DIR / ("validation_runs.jsonl" if validate else "external_validation_packets.jsonl")
    write_jsonl(output, records)
    if validate:
        write_jsonl(CLI_OUTPUT_DIR / "external_evidence_ledger.jsonl", evidence_ledger(records))
    print(f"Wrote {len(records)} records to {output.relative_to(CLI_OUTPUT_DIR.parent)}")
    return 0


def command_retrieve(args: argparse.Namespace) -> int:
    return _run(args, validate=False)


def command_validate(args: argparse.Namespace) -> int:
    return _run(args, validate=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query planning + hybrid retrieval + reranking + joint multi-paper entailment.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build-candidates", help="Read raw tuple files into candidate JSONL.")
    build.add_argument("--tuple-file", default="raw_triplets.txt")
    build.set_defaults(func=command_build_candidates)
    health = subparsers.add_parser("health-check", help="Verify OpenCode and configured task model chains.")
    health.set_defaults(func=command_health_check)
    for name, func, help_text in (("retrieve", command_retrieve, "Retrieve/rerank without judging."), ("validate", command_validate, "Run one-pass external validation.")):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--candidate-id")
        sub.add_argument("--limit", type=int)
        sub.add_argument("--articles-per-candidate", type=int, default=Settings().external_top_k, choices=range(1, 31))
        sub.add_argument("--top-chunks", type=int, default=Settings().external_top_chunks, choices=range(1, 13))
        sub.add_argument("--delay-seconds", type=float, default=0.5)
        sub.set_defaults(func=func)
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    raise SystemExit(args.func(args))
