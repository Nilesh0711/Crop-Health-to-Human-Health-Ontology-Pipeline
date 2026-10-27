"""Generate evaluation samples or score human-labelled JSONL without model calls."""
import argparse
import json
from pathlib import Path

from backend.stages.evaluation.metrics import evaluation_sample, score_evaluation
from backend.shared.storage import read_jsonl, write_jsonl


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='command', required=True)
    score = commands.add_parser('score', help='Score completed human gold fields')
    score.add_argument('path', type=Path)
    score.add_argument('--retrieval-k', type=int, default=3)
    sample = commands.add_parser('sample', help='Generate an unlabelled stratified sample')
    sample.add_argument('run_directory', type=Path)
    sample.add_argument('--output', type=Path)
    sample.add_argument('--limit', type=int, default=80)
    sample.add_argument('--overwrite', action='store_true', help='Explicitly replace an existing sample')
    args = parser.parse_args(argv)
    if args.command == 'score':
        if not args.path.is_file():
            parser.error(f'Evaluation file not found: {args.path}')
        if args.retrieval_k < 1:
            parser.error('--retrieval-k must be positive')
        print(json.dumps(score_evaluation(read_jsonl(args.path), args.retrieval_k), indent=2))
    else:
        candidates = args.run_directory / 'candidates.jsonl'
        if not candidates.is_file():
            parser.error(f'Candidate file not found: {candidates}')
        if args.limit < 1:
            parser.error('--limit must be positive')
        output = args.output or args.run_directory / 'evaluation_sample.jsonl'
        if output.exists() and not args.overwrite:
            parser.error(f'Refusing to replace {output}; choose --output or explicitly use --overwrite')
        rows = evaluation_sample(read_jsonl(candidates), read_jsonl(args.run_directory / 'validation_runs.jsonl'), args.limit)
        write_jsonl(output, rows)
        print(json.dumps({'output': str(output), 'records': len(rows)}))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
