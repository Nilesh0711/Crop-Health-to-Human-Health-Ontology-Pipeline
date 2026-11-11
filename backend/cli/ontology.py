"""Reference import, formal reasoning and independently labelled evaluation."""
import argparse
import json
from pathlib import Path

from backend.config.paths import ONTOLOGY_DIR
from backend.ontology.catalog import catalog, RESOURCE_DIR
from backend.ontology.bootstrap import acquire
from backend.ontology.importer import import_module
from backend.pipelines.ontology import rebuild
from backend.stages.ontology_reasoning.engine import validate_rule
from backend.stages.evaluation.grounding import export_sample, evaluate
from backend.stages.evaluation.grounding_pilot import prepare_pilot, run_pilot
from backend.shared.artifacts import save_json


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('sync-reference', help='Explicitly refresh the configured term fragments; changes invalidate mappings')
    sub.add_parser('inspect')
    sub.add_parser('reason', help='Rebuild from saved compatible mappings; no LLM calls')
    imp = sub.add_parser('import')
    imp.add_argument('--file', type=Path, required=True)
    imp.add_argument('--format', choices=['xml', 'turtle', 'nt'], default='turtle')
    imp.add_argument('--term', action='append', required=True)
    imp.add_argument('--note', required=True)
    rule = sub.add_parser('add-reviewed-rule')
    rule.add_argument('--file', type=Path, required=True)
    export = sub.add_parser('export-benchmark')
    export.add_argument('--output', type=Path, required=True)
    score = sub.add_parser('evaluate')
    score.add_argument('--labels', type=Path, required=True)
    score.add_argument('--current-predictions', action='store_true', help='Use saved mappings only when source/reference signatures match the review set')
    score.add_argument('--results-root', type=Path, help='Isolated pilot results directory; requires --current-predictions')
    pilot = sub.add_parser('prepare-pilot', help='Prepare a small unlabelled mention review set without any LLM calls')
    pilot.add_argument('--output', type=Path, required=True)
    pilot.add_argument('--document', help='Exact saved PDF folder name')
    pilot.add_argument('--limit', type=int, default=20)
    ground = sub.add_parser('ground-pilot', help='Run only grounding on frozen pilot sources; writes isolated results and makes paid model calls')
    ground.add_argument('--sample', type=Path, required=True)
    ground.add_argument('--output', type=Path, required=True)
    ground.add_argument('--thinking', choices=['on', 'off'], default='off')
    formats = sub.add_parser('compare-formats', help='Compare JSON and plain grounding rows on frozen saved claims; recovery is disabled')
    formats.add_argument('--document', required=True, help='Exact saved PDF folder name')
    formats.add_argument('--output', type=Path, required=True, help='New isolated directory outside ontology')
    formats.add_argument('--batch-size', type=int, default=6)
    formats.add_argument('--batches', type=int, default=3)
    formats.add_argument('--thinking', choices=['on', 'off'], default='off')
    args = parser.parse_args(argv)
    if args.command == 'sync-reference':
        result = acquire()
    elif args.command == 'import':
        result = import_module(args.file, args.term, args.note, format=args.format)
    elif args.command == 'add-reviewed-rule':
        rule_data = json.loads(args.file.read_text(encoding='utf-8'))
        ref = catalog()
        validate_rule(rule_data, ref)
        if any(r['id'] == rule_data['id'] for r in ref.profile['biological_rules']):
            raise ValueError('Rule ID already exists; review a new version explicitly')
        profile = dict(ref.profile)
        profile['biological_rules'] = [*profile['biological_rules'], rule_data]
        save_json(RESOURCE_DIR / 'profile.json', profile)
        result = rebuild(ONTOLOGY_DIR)[1]['counts']
    elif args.command == 'reason':
        result = rebuild(ONTOLOGY_DIR)[1]['counts']
    elif args.command == 'export-benchmark':
        result = export_sample(ONTOLOGY_DIR, args.output)
    elif args.command == 'evaluate':
        if args.results_root and not args.current_predictions:
            parser.error('--results-root requires --current-predictions')
        result = evaluate(args.labels, root=(args.results_root or ONTOLOGY_DIR) if args.current_predictions else None)
    elif args.command == 'prepare-pilot':
        result = prepare_pilot(ONTOLOGY_DIR, catalog(), args.output, document=args.document, limit=args.limit)
    elif args.command == 'compare-formats':
        from dataclasses import replace
        from backend.config.settings import Settings
        from backend.providers.llm import OpenCodeClient
        from backend.shared.storage import read_jsonl
        from backend.stages.evaluation.grounding_formats import compare_formats
        if Path(args.document).name != args.document or args.document in {'.', '..'}:
            parser.error('Document must be an exact saved folder name')
        if args.output.resolve().is_relative_to(ONTOLOGY_DIR.resolve()):
            parser.error('Format comparisons must be outside the active ontology workspace')
        claims = read_jsonl(ONTOLOGY_DIR / args.document / 'candidates.jsonl')
        settings = replace(Settings(), thinking={'pathway_assembly': args.thinking == 'on'},
                           json_repair_attempts=0, json_regeneration_attempts=0)
        reference = catalog()
        def client_factory(wire_format, log, response_dir):
            return OpenCodeClient(replace(settings, ontology_grounding_output_format=wire_format),
                task='pathway_assembly', on_request=log, response_dir=response_dir)
        result = compare_formats(claims, reference, reference, args.output, client_factory,
                                 batch_size=args.batch_size, batches=args.batches)
        print(json.dumps({k: v for k, v in result.items() if k != 'differences'}, ensure_ascii=False, indent=2))
        return 0
    elif args.command == 'ground-pilot':
        from dataclasses import replace
        from threading import Lock
        from backend.config.settings import Settings
        from backend.providers.llm import OpenCodeClient
        from backend.shared.storage import read_jsonl, file_hash, utc_now
        from backend.shared.artifacts import save_rows
        if args.output.resolve().is_relative_to(ONTOLOGY_DIR.resolve()):
            parser.error('Pilot results must be outside the active ontology workspace')
        settings = replace(Settings(), thinking={'pathway_assembly': args.thinking == 'on'})
        requests, lock = [], Lock()
        def log(metrics):
            with lock:
                requests.append({'at': utc_now(), **metrics})
                save_rows(args.output / 'llm_requests.jsonl', requests)
        client = OpenCodeClient(settings, task='pathway_assembly', response_dir=args.output / 'model_responses', on_request=log)
        reference = catalog()
        result = run_pilot(read_jsonl(args.sample), reference, args.output, client)
        result.update(model=client.model, thinking=args.thinking, requests=client.request_metrics,
                      catalog_signature=reference.signature, sample_sha256=file_hash(args.sample))
        save_json(args.output / 'pilot_summary.json', result)
    else:
        ref = catalog()
        result = {'profile': ref.profile, 'reference_terms': len(ref.terms), 'signature': ref.signature,
                  'coverage': ref.manifest['coverage']}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
