"""Offline replay of frozen development cases; never calls a model or edits runs."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path

from backend.domain.extraction_review import extraction_warnings, REVIEW_POLICY
from backend.stages.claim_extraction.anchoring import endpoint_anchor, anchor_diagnostic, ANCHORING_POLICY
from backend.stages.claim_extraction.contract import EXTRACTION_CONTRACT_VERSION
from backend.ontology.policies import GROUNDING_POLICY


def replay(sample):
    results = []
    for case in sample['cases']:
        claim, anchors = case['proposed_claim'], case['proposed_source_anchors']
        sentences = [s['text'] for s in case['source_sentences']]
        bindings = {}
        for role in ('subject', 'object'):
            match = endpoint_anchor(claim[role], anchors.get(role, claim[role]), sentences,
                                    case.get('abbreviation_definitions', []))
            bindings[role] = ({'supported': True, 'source_span': match[0], 'basis': match[1]}
                              if match else {'supported': False, 'category': anchor_diagnostic(
                                  claim[role], anchors.get(role, claim[role]), sentences)})
        supported = all(b['supported'] for b in bindings.values())
        warnings = extraction_warnings(case['review_claim'])
        expected = case.get('expected_anchor_support')
        results.append({'case_id': case['case_id'], 'document': case['document'], 'anchors': bindings,
            'anchor_supported': supported, 'baseline_accepted': case['baseline_accepted'],
            'expected_anchor_support': expected,
            'anchor_expectation_met': supported == expected if expected is not None else None,
            'warnings': warnings, 'baseline_grounding': case.get('baseline_grounding'),
            'scientific_review_status': 'pending', 'model_comparison': 'not_run'})
    return {'purpose': 'Development replay, not independent scientific accuracy; no model calls.',
            'sample_sha256': hashlib.sha256(json.dumps(sample, sort_keys=True, ensure_ascii=False).encode('utf-8')).hexdigest(),
            'policies': {'extraction': EXTRACTION_CONTRACT_VERSION, 'anchoring': ANCHORING_POLICY,
                         'review': REVIEW_POLICY, 'grounding': GROUNDING_POLICY},
            'summary': {'cases': len(results), 'documents': dict(Counter(r['document'] for r in results)),
                'anchor_supported': sum(r['anchor_supported'] for r in results),
                'baseline_rejected_now_supported': sum(r['anchor_supported'] and not r['baseline_accepted'] for r in results),
                'anchor_expectation_failures': sum(r['anchor_expectation_met'] is False for r in results),
                'warning_cases': sum(bool(r['warnings']) for r in results), 'model_calls': 0},
            'cases': results}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--sample', type=Path, default=Path('tests/fixtures/atomic_grounding_v14_review.json'))
    parser.add_argument('--output', type=Path, required=True, help='New isolated directory outside ontology and published data')
    args = parser.parse_args(argv)
    root, output = Path(__file__).resolve().parents[2], args.output.resolve()
    protected = [root / 'ontology', root / 'deploy', root / 'archive']
    if any(output == p.resolve() or output.is_relative_to(p.resolve()) for p in protected):
        parser.error('Replay output must be outside saved runs, deployments and backups')
    if output.exists():
        parser.error('Use a new output directory to preserve prior reports')
    result = replay(json.loads(args.sample.read_text(encoding='utf-8')))
    output.mkdir(parents=True)
    (output / 'report.json').write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding='utf-8')
    lines = ['# Offline development replay', '', result['purpose'], '',
             'Model comparison and scientific review remain pending. Recovered anchors are not accuracy labels.', '',
             '| Case | Source anchors supported | Baseline accepted | Review warnings |',
             '|---|---|---|---|']
    for row in result['cases']:
        warnings = ', '.join(w['code'] for w in row['warnings']) or 'none'
        lines.append(f"| {row['case_id']} | {row['anchor_supported']} | {row['baseline_accepted']} | {warnings} |")
    (output / 'report.md').write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print(json.dumps(result['summary']))
    return 1 if result['summary']['anchor_expectation_failures'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
