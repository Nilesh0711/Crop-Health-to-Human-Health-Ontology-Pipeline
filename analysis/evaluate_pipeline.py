"""Create or score a stratified claim-evaluation file."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.evaluation import read_jsonl, score_evaluation, write_evaluation_sample


def main() -> None:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    sample = subparsers.add_parser("sample")
    sample.add_argument("run_dir", type=Path)
    sample.add_argument("--limit", type=int, default=80)
    score = subparsers.add_parser("score")
    score.add_argument("labels", type=Path)
    score.add_argument("--retrieval-k", type=int, default=3)
    args = parser.parse_args()
    if args.command == "sample":
        output = write_evaluation_sample(args.run_dir, read_jsonl(args.run_dir / "candidates.jsonl"),
                                         read_jsonl(args.run_dir / "validation_runs.jsonl"), args.limit)
        print(output)
    else:
        print(json.dumps(score_evaluation(read_jsonl(args.labels), args.retrieval_k), indent=2))


if __name__ == "__main__":
    main()

