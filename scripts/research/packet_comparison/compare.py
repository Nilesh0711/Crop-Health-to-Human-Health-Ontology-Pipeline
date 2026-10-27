"""Paired, resumable real-provider extraction experiment; never writes ontology data.

Prepare immutable <case>.prepared.json streams, then run this script with --report.
Results are source-fidelity proxies, not expert scientific ground truth. Same
model, prompt, claim cap, output budget and recovery policy apply to both arms.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import random
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from backend.config.settings import Settings
from backend.providers.llm import OpenCodeClient
from backend.shared.storage import utc_now, file_hash
from backend.stages.claim_extraction.extractor import extract_claims
from backend.stages.pdf_parsing.packets import pdf_bibliography, source_article_identifiers
from backend.stages.pdf_parsing.packet_experiments import coverage_report


def dump(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')
    temporary.replace(path)


def make_plan(cases, repeats, seed, first_repeat=1):
    """Interleave randomized arm order within each paper/repeat pair."""
    rng = random.Random(seed)
    pairs = [(case, repeat) for repeat in range(first_repeat, first_repeat+repeats) for case in cases]
    rng.shuffle(pairs)
    plan = []
    for case, repeat in pairs:
        arms = list(case['arms'])
        rng.shuffle(arms)
        for index in range(max(map(len, case['arms'].values()))):
            for arm in arms:
                packets = case['arms'][arm]
                if index < len(packets):
                    plan.append(dict(case=case['id'], arm=arm, repeat=repeat,
                                     packet=packets[index]['packet_number']))
    return plan


def job_key(job):
    return f"{job['case']}/{job['arm']}/repeat_{job['repeat']}/packet_{job['packet']:02d}"


def run(report, repeats=2, workers=2, seed=24102026, first_repeat=1):
    cases = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(report.glob('*.prepared.json'))]
    if not cases:
        raise ValueError('No prepared cases found')
    fingerprints=report/'pdf_fingerprints.json'
    expected_pdfs=json.loads(fingerprints.read_text(encoding='utf-8')) if fingerprints.exists() else {}
    for case in cases:
        if case['id'] in expected_pdfs and file_hash(Path(case['pdf']))!=expected_pdfs[case['id']]:
            raise ValueError(f"Source PDF changed since corpus preparation: {case['id']}")
    targets=report/'reference_targets.json'
    if not targets.exists():
        raise ValueError('Freeze source reference targets before running the experiment')
    scientific_inputs=dict(pdf_sha256=expected_pdfs,reference_targets_sha256=file_hash(targets))
    frozen=report/'scientific_inputs.json'
    if frozen.exists():
        if json.loads(frozen.read_text(encoding='utf-8'))!=scientific_inputs:
            raise ValueError('Scientific inputs changed; use a fresh report directory')
    else:
        dump(frozen,scientific_inputs)
    settings = replace(Settings(), extraction_min_output_budget=12288,
                       regeneration_max_output_tokens=32768, extraction_claims_per_packet=4)
    plan = make_plan(cases, repeats, seed, first_repeat)
    inputs = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(report.glob('*.prepared.json'))}
    declaration = dict(seed=seed, repeats=repeats, workers=workers,
        model=settings.extraction_model, output_budget=12288, claim_slots_per_packet=4,
        regeneration_cap=32768, thinking='provider_default', inputs=inputs, plan=plan,
        decision_rule='Quality first: sentence coverage and chapter isolation required; compare source fidelity, fixed reference anchors, qualifier/boundary errors, recovery incidence, and tokens/time per entailed unique claim. Keep baseline if quality evidence is mixed or inconclusive.',
        limitations=['No expert gold annotations; verifier uses the same model family.',
                     'Smaller packets have more claim slots; raw claim count is not recall.',
                     'Three documents/excerpts and two repeats do not establish general superiority.'],
        coverage={case['id']: {arm: coverage_report(case['stream'], packets)
                  for arm, packets in case['arms'].items()} for case in cases})
    manifest = report/'manifest.json'
    if manifest.exists():
        if json.loads(manifest.read_text(encoding='utf-8'))['experiment'] != declaration:
            raise ValueError('Experiment differs from saved manifest; use a new report directory')
    else:
        dump(manifest, dict(created_at=utc_now(), experiment=declaration))
    lookup = {case['id']:case for case in cases}
    for case in cases:
        cache = report/f"{case['id']}.references.json"
        if not cache.exists():
            pdf = Path(case['pdf'])
            dump(cache, dict(bibliography=pdf_bibliography(pdf), identifiers=source_article_identifiers(pdf)))
        case['references'] = json.loads(cache.read_text(encoding='utf-8'))
    pending = []
    for job in plan:
        folder = report/'extraction'/job_key(job)
        if (folder/'result.json').exists():
            continue
        if (folder/'started.json').exists():
            raise ValueError(f'Uncertain interrupted request at {folder}; inspect before repeating paid work')
        pending.append(job)

    def execute(job):
        folder = report/'extraction'/job_key(job)
        case = lookup[job['case']]
        packet = next(p for p in case['arms'][job['arm']] if p['packet_number']==job['packet'])
        dump(folder/'started.json', dict(job=job, started_at=utc_now()))
        attempts, events, packets = [], [], []
        def on_request(metrics):
            attempts.append(metrics)
            dump(folder/'attempts.json', attempts)
        client = OpenCodeClient(settings, task='extraction', on_request=on_request,
                                response_dir=folder/'responses')
        started = time.monotonic()
        result = dict(job=job, started_at=utc_now())
        try:
            candidates, rejected, rejection_records = extract_claims(Path(case['pdf']), client, 0,
                lambda level,message: events.append(dict(level=level,message=message)), packets=[packet],
                claims_per_packet=4, packet_results=packets,
                source_identifiers=case['references']['identifiers'], bibliography=case['references']['bibliography'])
            result.update(candidates=candidates, rejected=rejected, rejection_records=rejection_records,
                          status='completed' if all(p['status']=='completed' for p in packets) else 'failed')
        except Exception as exc:
            result.update(status='failed', error=f'{type(exc).__name__}: {exc}', candidates=[])
        result.update(elapsed_seconds=round(time.monotonic()-started,3), finished_at=utc_now(),
                      attempts=attempts, events=events, packets=packets, metrics=client.request_metrics)
        dump(folder/'result.json', result)
        return dict(job=job,status=result['status'],claims=len(result['candidates']),seconds=result['elapsed_seconds'],
                    attempts=len(attempts),output_tokens=result['metrics']['output_tokens'])

    print(json.dumps(dict(stage='extraction', pending=len(pending), total=len(plan), workers=workers)),flush=True)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures=[pool.submit(execute,job) for job in pending]
        for done,future in enumerate(as_completed(futures),1):
            print(json.dumps(dict(done=done,**future.result())),flush=True)
    print(json.dumps(dict(stage='extraction_finished',finished_at=utc_now())),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--repeats',type=int,default=2)
    parser.add_argument('--workers',type=int,choices=range(1,3),default=2)
    parser.add_argument('--first-repeat',type=int,default=1)
    args=parser.parse_args()
    if min(args.repeats,args.first_repeat)<1:
        parser.error('--repeats and --first-repeat must be positive')
    run(args.report,args.repeats,args.workers,first_repeat=args.first_repeat)
