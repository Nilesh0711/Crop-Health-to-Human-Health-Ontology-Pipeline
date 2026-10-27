"""Replay archived answers through production extraction/deduplication without HTTP.

Packet calls run independently for checkpointing. This replay restores the exact
cross-packet duplicate handling of one normal extract_claims invocation. Missing
provider answers stay missing; replay never invents or requests replacements.
"""
from __future__ import annotations
import argparse
from collections import defaultdict
from copy import deepcopy
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from backend.providers.llm import LLMError
from backend.stages.claim_extraction.extractor import extract_claims
from backend.stages.claim_extraction.deduplication import deduplicate_source_claims
from scripts.research.packet_comparison.compare import dump


def replay(report):
    grouped=defaultdict(list)
    for path in (report/'extraction').glob('**/result.json'):
        result=json.loads(path.read_text(encoding='utf-8'))
        job=result['job']
        grouped[(job['case'],job['arm'],job['repeat'])].append((path,result))
    cases={}
    for (case_id,arm,repeat),records in grouped.items():
        if case_id not in cases:
            cases[case_id]=json.loads((report/f'{case_id}.prepared.json').read_text(encoding='utf-8'))
        case=cases[case_id]
        records.sort(key=lambda entry:entry[1]['job']['packet'])
        answers=[]
        for path,result in records:
            accepted=[json.loads(p.read_text(encoding='utf-8')) for p in (path.parent/'responses').glob('*.json')]
            accepted=[a for a in accepted if a['status']=='ok' and a.get('accepted_response') is not None]
            if result['status']=='completed':
                if len(accepted)!=1:
                    raise ValueError(f'No unique accepted provider answer at {path}')
                answers.append(accepted[0]['accepted_response'])
            else:
                answers.append(None)
        class ReplayOnlyClient:
            def chat_json(self,*args,**kwargs):
                answer=answers.pop(0)
                if answer is None:
                    raise LLMError('Research replay: original provider answer unavailable; no new request')
                return deepcopy(answer)
        packet_ids={r['job']['packet'] for _,r in records}
        packets=[p for p in case['arms'][arm] if p['packet_number'] in packet_ids]
        references=json.loads((report/f'{case_id}.references.json').read_text(encoding='utf-8'))
        events=[]
        candidates,rejected,rejections=extract_claims(Path(case['pdf']),ReplayOnlyClient(),0,
            lambda level,message:events.append(dict(level=level,message=message)),packets=packets,
            claims_per_packet=4,bibliography=references['bibliography'],source_identifiers=references['identifiers'])
        candidates,canonical_duplicates=deduplicate_source_claims(candidates)
        assert not answers
        dump(report/'grouped'/case_id/arm/f'repeat_{repeat}'/'result.json',dict(
            job=dict(case=case_id,arm=arm,repeat=repeat),candidates=candidates,
            packet_job_count=len(records),
            canonical_duplicates=canonical_duplicates,rejected=rejected,rejections=rejections,
            complete=len(records)==len(case['arms'][arm]) and all(r['status']=='completed' for _,r in records),
            events=events,method='Offline replay of accepted responses through unmodified production extraction and canonical deduplication.'))
    print(json.dumps(dict(grouped_replays=len(grouped))))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True,type=Path)
    replay(parser.parse_args().report)
