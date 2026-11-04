"""Commands for an OpenCode evidence-validation run with one primary model per task."""

from __future__ import annotations

import argparse
import json

from backend.cli.candidates import build_candidates
from backend.config.settings import CLI_OUTPUT_DIR, Settings
from backend.providers.llm import OpenCodeClient
from backend.shared.storage import write_jsonl


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
    ready['external'] = ready['external'] and settings.external_paper_model in names
    print(json.dumps({"opencode_reachable": True, 'task_models': settings.task_models, 'task_ready': ready,
                      'external_phase_models': settings.external_phase_models,
                      'external_judgement_policy': 'pathway_only',
                      'external_top_k': settings.external_top_k,
                      'external_top_chunks': settings.external_top_chunks,
                      'model_policy': 'primary_only', "models": names}, indent=2))
    return 0 if all(ready.values()) else 2


def command_validate(args):
    from pathlib import Path
    from backend.pipelines.document import run_pipeline
    result = run_pipeline(Path(args.pdf),args.max_pages,args.papers,
        lambda level,message:print(f'{level}: {message}',flush=True),resume=args.resume,
        neighbor_max_pairs=args.pairs)
    print(json.dumps(result,indent=2))
    return 0


def build_parser():
    parser = argparse.ArgumentParser(description='Neighboring-claim validation after source-grounded assembly.')
    sub = parser.add_subparsers(dest='command',required=True)
    health = sub.add_parser('health-check')
    health.set_defaults(func=command_health_check)
    build = sub.add_parser('build-candidates',help='Offline import of raw tuples; no external validation.')
    build.add_argument('--tuple-file',default='raw_triplets.txt')
    build.set_defaults(func=command_build_candidates)
    validate = sub.add_parser('validate',help='Run/resume source checks and local connection assessment.')
    validate.add_argument('--pdf',required=True)
    validate.add_argument('--resume',action='store_true')
    validate.add_argument('--max-pages',type=int,default=0)
    validate.add_argument('--papers',type=int,default=8,choices=range(1,31))
    validate.add_argument('--pairs',type=int,default=100,choices=range(1,501))
    validate.set_defaults(func=command_validate)
    return parser


if __name__=='__main__':
    args=build_parser().parse_args()
    raise SystemExit(args.func(args))
