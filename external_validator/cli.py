"""Commands for a clean, local-Qwen validation run."""

from __future__ import annotations

import argparse
import json
import time
from typing import Any

from .candidates import build_candidates
from .config import RAG_DIR, Settings
from .llm import OllamaClient
from .nli import NLIValidator
from .rerank import SemanticReranker
from .storage import read_jsonl, write_jsonl
from .workflow import evidence_ledger, retrieve_only, validate_candidate


def _selected_candidates(args: argparse.Namespace) -> list[dict[str, Any]]:
    items = read_jsonl(RAG_DIR / "candidates.jsonl")
    if args.candidate_id:
        items = [item for item in items if item["candidate_id"] == args.candidate_id]
    if args.limit is not None:
        items = items[:args.limit]
    if not items:
        raise SystemExit("No candidates selected. Run build-candidates first or choose a valid candidate ID.")
    return items


def command_build_candidates(args: argparse.Namespace) -> int:
    candidates, documents, errors = build_candidates(args.tuple_file)
    write_jsonl(RAG_DIR / "candidates.jsonl", candidates)
    write_jsonl(RAG_DIR / "documents.jsonl", documents)
    write_jsonl(RAG_DIR / "candidate_parse_errors.jsonl", errors)
    print(f"Wrote {len(candidates)} candidates, {len(documents)} documents, and {len(errors)} parse errors.")
    return 0


def command_health_check(_: argparse.Namespace) -> int:
    settings = Settings()
    models = OllamaClient(settings).health_check().get("models", [])
    names = [model.get("name") for model in models]
    print(json.dumps({"ollama_reachable": True, "configured_model": settings.model, "model_present": settings.model in names, "models": names}, indent=2))
    return 0 if settings.model in names else 2


def _run(args: argparse.Namespace, validate: bool) -> int:
    settings = Settings()
    client, reranker = OllamaClient(settings), SemanticReranker(settings)
    client.health_check()
    nli_validator = NLIValidator(settings) if validate and settings.enable_nli else None
    if nli_validator is not None:
        nli_validator.warmup()
    records: list[dict[str, Any]] = []
    candidates = _selected_candidates(args)
    for position, candidate in enumerate(candidates, 1):
        print(f"[{position}/{len(candidates)}] {candidate['raw_tuple']}")
        try:
            result = (validate_candidate(
                candidate, client, reranker, settings,
                args.articles_per_candidate, args.top_chunks,
                nli_validator=nli_validator,
            ) if validate else retrieve_only(
                candidate, client, reranker, settings,
                args.articles_per_candidate, args.top_chunks,
            ))
        except Exception as exc:
            result = {"candidate_id": candidate["candidate_id"], "candidate": candidate, "validation_status": "pipeline_error", "error": str(exc)}
        records.append(result)
        if args.delay_seconds and position < len(candidates):
            time.sleep(args.delay_seconds)
    output = RAG_DIR / ("validation_runs.jsonl" if validate else "external_validation_packets.jsonl")
    write_jsonl(output, records)
    if validate:
        write_jsonl(RAG_DIR / "external_evidence_ledger.jsonl", evidence_ledger(records))
    print(f"Wrote {len(records)} records to {output.relative_to(RAG_DIR.parent)}")
    return 0


def command_retrieve(args: argparse.Namespace) -> int:
    return _run(args, validate=False)


def command_validate(args: argparse.Namespace) -> int:
    return _run(args, validate=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Qwen query translation + HyDE + hybrid retrieval + cross-encoder reranking + per-document entailment.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    build = subparsers.add_parser("build-candidates", help="Read raw tuple files into candidate JSONL.")
    build.add_argument("--tuple-file", default="raw_triplets.txt")
    build.set_defaults(func=command_build_candidates)
    health = subparsers.add_parser("health-check", help="Verify local Ollama and the configured Qwen model.")
    health.set_defaults(func=command_health_check)
    for name, func, help_text in (("retrieve", command_retrieve, "Retrieve/rerank without judging."), ("validate", command_validate, "Run one-pass external validation.")):
        sub = subparsers.add_parser(name, help=help_text)
        sub.add_argument("--candidate-id")
        sub.add_argument("--limit", type=int)
        sub.add_argument("--articles-per-candidate", type=int, default=4)
        sub.add_argument("--top-chunks", type=int, default=3)
        sub.add_argument("--delay-seconds", type=float, default=0.5)
        sub.set_defaults(func=func)
    return parser


if __name__ == "__main__":
    args = build_parser().parse_args()
    raise SystemExit(args.func(args))
