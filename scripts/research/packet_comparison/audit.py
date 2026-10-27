"""Source-check every distinct full candidate input from the paired experiment.

This is a model-based source-fidelity audit, not independent expert gold.
No arm label is sent to the source checker. Exact-input caching only; different
wording, qualifiers, packet context or local context get separate judgements.
"""
from __future__ import annotations
import argparse
import hashlib
import json
import re
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from backend.config.settings import Settings
from backend.providers.llm import OpenCodeClient
from backend.shared.batching import iter_batched_judgments
from backend.shared.storage import utc_now
from backend.stages.source_validation.checker import validate_source_claim
from scripts.research.packet_comparison.compare import dump


def candidate_key(candidate):
    return hashlib.sha256(json.dumps(candidate,ensure_ascii=False,sort_keys=True).encode()).hexdigest()


def transport_failed(review):
    """Only an unavailable transport answer can enter the separate remeasurement."""
    text=str(review.get('rationale','')).casefold()
    return review.get('decision')=='audit_error' and (
        'connection failed' in text or 'timed out' in text or
        bool(re.search(r'http 5\d\d\b',text)))


def audit(report,watch=False,grouped=False,remeasure_errors=False):
    manifest=json.loads((report/'manifest.json').read_text(encoding='utf-8'))
    expected=(len({(j['case'],j['arm'],j['repeat']) for j in manifest['experiment']['plan']})
              if grouped else len(manifest['experiment']['plan']))
    settings=Settings()
    result_dir='source_audit_remeasurement' if remeasure_errors else 'source_audit'
    protocol_name=('source_remeasurement_protocol.json' if remeasure_errors else
                   'grouped_audit_protocol.json' if grouped else 'audit_protocol.json')
    dump(report/protocol_name,dict(created_at=utc_now(),model=settings.source_model,
        batch_size=3,workers=1,selection='All distinct full candidate inputs, including wording and full context.',
        arm_blinding='No layout/arm identifier in model prompt; source text and generated claims can reveal differences.',
        role='Source fidelity proxy only, same model family as extraction; not scientific ground truth.',
        concurrent_work='Up to 2 extraction calls plus 1 source-audit call; timing is observed under this load.'))
    while True:
        results=[json.loads(p.read_text(encoding='utf-8')) for p in sorted((report/('grouped' if grouped else 'extraction')).glob('**/result.json'))]
        candidates={}
        for result in results:
            for candidate in result['candidates']:
                key=candidate_key(candidate)
                original=report/'source_audit'/f'{key}.json'
                if remeasure_errors:
                    if (original.exists() and transport_failed(json.loads(original.read_text(encoding='utf-8'))['decision'])
                            and not (report/result_dir/f'{key}.json').exists()):
                        candidates[key]=candidate
                elif not original.exists():
                    candidates[key]=candidate
        if len(candidates)<3 and len(results)<expected and watch:
            time.sleep(2)
            continue
        keys=sorted(candidates)
        for offset in range(0,len(keys),3):
            group=keys[offset:offset+3]
            batch_id=hashlib.sha256('|'.join(group).encode()).hexdigest()[:24]
            folder=report/('source_remeasurement_batches' if remeasure_errors else 'source_audit_batches')/batch_id
            if (folder/'started.json').exists() and not (folder/'finished.json').exists():
                raise ValueError(f'Uncertain audit request at {folder}; inspect before repeating paid work')
            inputs=[]
            for key in group:
                candidate=dict(candidates[key])
                candidate['candidate_id']='audit-'+key
                inputs.append(candidate)
            dump(folder/'started.json',dict(keys=group,started_at=utc_now()))
            attempts=[]
            def on_request(metrics):
                attempts.append(metrics)
                dump(folder/'attempts.json',attempts)
            client=OpenCodeClient(settings,task='source',on_request=on_request,response_dir=folder/'responses')
            started=time.monotonic()
            checked=[]
            try:
                checked=list(iter_batched_judgments(inputs,client,validate_source_claim,batch_size=3,label='Blinded source fidelity audit'))
            except Exception as exc:
                checked=[(c,dict(candidate_id=c['candidate_id'],decision='audit_error',rationale=f'{type(exc).__name__}: {exc}')) for c in inputs]
            for candidate,decision in checked:
                key=candidate['candidate_id'].removeprefix('audit-')
                dump(report/result_dir/f'{key}.json',dict(key=key,original_candidate_id=candidates[key]['candidate_id'],
                    decision=decision,finished_at=utc_now(),batch_id=batch_id))
            summary=dict(batch_id=batch_id,finished_at=utc_now(),seconds=round(time.monotonic()-started,3),
                         attempts=attempts,metrics=client.request_metrics,
                         decisions=[decision.get('decision') for _,decision in checked])
            dump(folder/'finished.json',summary)
            print(json.dumps({k:v for k,v in summary.items() if k not in {'attempts','metrics'}}),flush=True)
        if not watch or len(results)>=expected:
            if not candidates:
                break
            continue
        time.sleep(2)
    print(json.dumps(dict(stage='source_audit_finished',finished_at=utc_now())),flush=True)


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True,type=Path)
    parser.add_argument('--watch',action='store_true')
    parser.add_argument('--grouped',action='store_true')
    parser.add_argument('--remeasure-errors',action='store_true',help='One separate measurement of transport-failed audit inputs; preserves original failures')
    args=parser.parse_args()
    audit(args.report,args.watch,args.grouped,args.remeasure_errors)
