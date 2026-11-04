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
    snapshot = commands.add_parser('snapshot-neighbors', help='Freeze saved pair results for offline comparison')
    snapshot.add_argument('run_directory', type=Path)
    snapshot.add_argument('--output', type=Path, required=True)
    compare = commands.add_parser('compare-neighbors', help='Compare two saved snapshots without inference or gold transfer')
    compare.add_argument('before', type=Path)
    compare.add_argument('after', type=Path)
    corpus = commands.add_parser('compare-corpus',help='Compare thematic document snapshots from a manifest')
    corpus.add_argument('manifest',type=Path)
    layout = commands.add_parser('audit-layout',help='Compare original and column-aware parsing without inference')
    layout.add_argument('pdfs',type=Path,nargs='+')
    layout.add_argument('--output',type=Path,required=True)
    args = parser.parse_args(argv)
    if args.command in {'audit-layout','compare-corpus'}:
        from backend.stages.evaluation.parsing import compare_layouts
        from backend.stages.evaluation.neighbors import compare_corpus
        from backend.shared.artifacts import save_json
        try:
            result = compare_layouts(args.pdfs) if args.command=='audit-layout' else compare_corpus(args.manifest)
            if args.command=='audit-layout':
                if args.output.exists() or args.output.resolve() in {p.resolve() for p in args.pdfs}:
                    raise ValueError('Choose a new report path, separate from the source PDFs')
                save_json(args.output,result)
                print(json.dumps({'output':str(args.output),'documents':len(result['documents'])}))
            else:
                print(json.dumps(result,indent=2))
        except (ValueError,OSError,KeyError) as exc:
            parser.error(str(exc))
        return 0
    if args.command in {'snapshot-neighbors', 'compare-neighbors'}:
        from backend.stages.evaluation.neighbors import snapshot_neighbors, compare_neighbors
        try:
            result = (snapshot_neighbors(args.run_directory, args.output) if args.command == 'snapshot-neighbors'
                      else compare_neighbors(args.before, args.after))
        except (ValueError, OSError) as exc:
            parser.error(str(exc))
        print(json.dumps(result, indent=2))
        return 0
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
