"""Prepare/run a small isolated extraction context comparison on saved packets."""
import argparse
from dataclasses import replace
from pathlib import Path
import json

from backend.config.paths import ONTOLOGY_DIR
from backend.config.settings import Settings
from backend.ontology.catalog import catalog
from backend.providers.llm import OpenCodeClient
from backend.shared.storage import read_jsonl
from backend.stages.evaluation.extraction_context import prepare_comparison, run_comparison


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--document', required=True, help='Exact saved document folder name')
    parser.add_argument('--targets', type=int, nargs='+', required=True, help='1–10 existing target numbers')
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--thinking', choices=['on', 'off'], help='Extraction thinking; defaults to the current application setting')
    parser.add_argument('--run', action='store_true', help='Also make paid first-pass model calls; otherwise only freeze/inspect inputs')
    parser.add_argument('--compare-format', action='store_true', help='Compare claim objects with nine-position JSON rows using identical local context')
    args = parser.parse_args(argv)
    if Path(args.document).name != args.document or args.document in {'.', '..'}:
        parser.error('Use an exact saved document folder name')
    packets = read_jsonl(ONTOLOGY_DIR / args.document / 'extraction_contexts.jsonl')
    metadata = prepare_comparison(packets, catalog(), args.output, targets=args.targets,
        source_title=args.document, document_id=args.document)
    print(json.dumps(metadata, ensure_ascii=True), flush=True)
    if args.run:
        defaults = Settings()
        enabled = defaults.task_thinking['extraction'] if args.thinking is None else args.thinking == 'on'
        settings = replace(defaults, thinking={**defaults.task_thinking, 'extraction': enabled},
            extraction_min_output_budget=50000, json_repair_attempts=0, json_regeneration_attempts=0)
        def factory(log, directory):
            return OpenCodeClient(settings, task='extraction', on_request=log, response_dir=directory)
        runner = run_comparison
        if args.compare_format:
            from backend.stages.evaluation.extraction_format import run_comparison as compare_format
            runner = compare_format
        report = runner(args.output, factory, emit=lambda message: print(message, flush=True))
        print(json.dumps(report['summary'], ensure_ascii=True), flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
