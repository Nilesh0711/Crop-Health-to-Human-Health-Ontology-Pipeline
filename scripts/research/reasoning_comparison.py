"""One paired pilot of provider-default versus disabled DeepSeek reasoning.

Only research requests receive the override; production settings are unchanged.
No hidden reasoning text or credentials are stored. Same-model structured-output
recovery and source-fidelity guards remain in use in both arms.
"""
from __future__ import annotations
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
from copy import deepcopy
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import sys
import time

sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
from backend.config.settings import Settings
from backend.providers.inference import InferenceHTTP
from backend.providers.llm import OpenCodeClient
from backend.shared.batching import iter_batched_judgments
from backend.shared.storage import utc_now,file_hash
from backend.stages.claim_extraction.extractor import extract_claims
from backend.stages.claim_extraction.deduplication import deduplicate_source_claims
from backend.stages.source_validation.checker import validate_source_claim
from scripts.research.packet_comparison.compare import dump


class ResearchThinkingHTTP(InferenceHTTP):
    """Inject exactly one documented parameter without changing production HTTP."""
    def __init__(self,settings,thinking):
        if thinking not in {'provider_default','disabled'}:
            raise ValueError('Research thinking must be provider_default or disabled')
        super().__init__(settings)
        self.thinking=thinking

    def _request(self,method,url,**kwargs):
        if method=='POST' and self.thinking=='disabled':
            payload=deepcopy(kwargs.get('json',{}))
            if payload.get('model')!='deepseek-v4.1-flash':
                raise ValueError('This experiment is restricted to DeepSeek v4.1 Flash')
            payload['thinking']={'type':'disabled'}
            kwargs={**kwargs,'json':payload}
        return super()._request(method,url,**kwargs)


class TargetedExtractionClient(OpenCodeClient):
    """Keep all reading context but constrain the extraction evidence region."""
    def __init__(self,*args,target_sentence_numbers,**kwargs):
        super().__init__(*args,**kwargs)
        self.target_sentence_numbers=tuple(target_sentence_numbers)

    def chat_json(self,system,prompt,**kwargs):
        from backend.providers.structured_output import IncompleteResponseError
        check=kwargs.get('response_check')
        target=set(self.target_sentence_numbers)
        def check_target(response):
            if check:
                check(response)
            for claim in response['claims']:
                numbers=claim.get('evidence_sentence_numbers',[])
                if not numbers or not set(numbers).issubset(target):
                    raise IncompleteResponseError('Returned evidence lies outside the extraction target')
        system += ('\nSPECIFIC EXTRACTION TARGET: Extract only edges whose direct evidence lies in '
                   f'sentence IDs {list(self.target_sentence_numbers)}. This overrides the global priority '
                   'index for selecting evidence. The entire original packet remains reading context for '
                   'explicit referents, modality, restrictions and headings. Do not extract unrelated facts '
                   'from that context. Do not invent or force an edge if the controlled vocabulary cannot '
                   'represent it. An empty claims array is valid. Preserve all existing evidence/schema rules.')
        return super().chat_json(system,prompt,**{**kwargs,'response_check':check_target})


def run(report,prepared,targeted=False):
    settings=replace(Settings(),extraction_min_output_budget=12288,
                     regeneration_max_output_tokens=32768,extraction_claims_per_packet=4)
    if settings.extraction_model!='opencode-go/deepseek-v4.1-flash':
        raise ValueError('The paired test requires the selected DeepSeek extraction model')
    cases=[]
    selections=[('crop_health',[2])] if targeted else [('crop_health',[1,2,3]),('soil_health_summary',[2])]
    for key,packet_numbers in selections:
        case=json.loads((prepared/f'{key}.prepared.json').read_text(encoding='utf-8'))
        refs=json.loads((prepared/f'{key}.references.json').read_text(encoding='utf-8'))
        expected=json.loads((prepared/'pdf_fingerprints.json').read_text(encoding='utf-8'))[key]
        if file_hash(Path(case['pdf']))!=expected:
            raise ValueError(f'Source changed: {key}')
        packets=[p for p in case['arms']['current_4000'] if p['packet_number'] in packet_numbers]
        if len(packets)!=len(packet_numbers):
            raise ValueError(f'Missing selected packet in {key}')
        target=[]
        if targeted:
            # Predeclare this exploratory paragraph before either follow-up call.
            # It contains packet 2's mycotoxin sentence, under the FHB heading.
            paragraph=packets[0]['sentences'][62]['paragraph_id']
            target=[i for i,sentence in enumerate(packets[0]['sentences'],1)
                    if sentence['paragraph_id']==paragraph]
        cases.append(dict(id=key,pdf=case['pdf'],pdf_sha256=expected,packets=packets,
                          references=refs,target_sentence_numbers=target))
    declaration=dict(created_at=utc_now(),model=settings.extraction_model,
        cases=[dict(id=c['id'],pdf=c['pdf'],pdf_sha256=c['pdf_sha256'],
                    packet_numbers=[p['packet_number'] for p in c['packets']],
                    packets_sha256=hashlib.sha256(json.dumps(c['packets'],sort_keys=True).encode()).hexdigest()) for c in cases],
        experiment='exploratory targeted follow-up' if targeted else 'original paired pilot',
        target_sentence_numbers={c['id']:c['target_sentence_numbers'] for c in cases},
        arms=['provider_default','disabled'],repeats=1,workers=2,initial_generation_limit=12288,
        claims_per_packet=4,regeneration_cap=32768,
        decision_rule='Pilot only. Confirm disabled reasoning with provider usage, compare source-grounded distinct claims and restrictions, duplicates, observed latency and actual generated tokens. Do not change production defaults or claim equivalence from one repeat.',
        limitations=['Same-model-family source audit is a proxy, not expert gold.',
                     'One repeat does not establish equivalent accuracy.',
                     'Temperature is held at the current request value; provider thinking mode may change whether that parameter takes effect.',
                     'Source verification keeps provider-default reasoning; only extraction is changed.'])
    if (report/'manifest.json').exists():
        raise ValueError('A pilot already exists here; inspect its checkpoints instead of repeating paid work')
    dump(report/'manifest.json',declaration)
    dump(report/'prepared_inputs.json',cases)

    def execute(case,arm):
        folder=report/case['id']/arm
        dump(folder/'started.json',dict(started_at=utc_now(),arm=arm))
        attempts,events,packet_results=[],[],[]
        def metrics(row):
            row=dict(row)
            row.update(requested_thinking=arm,thinking_control='explicit research override' if arm=='disabled' else 'provider-controlled')
            attempts.append(row)
            dump(folder/'attempts.json',attempts)
        client_class=TargetedExtractionClient if targeted else OpenCodeClient
        extra={'target_sentence_numbers':case['target_sentence_numbers']} if targeted else {}
        client=client_class(settings,task='extraction',on_request=metrics,response_dir=folder/'responses',**extra)
        client._inference=ResearchThinkingHTTP(client.settings,arm)
        started=time.monotonic()
        result=dict(case=case['id'],arm=arm,started_at=utc_now())
        try:
            raw,rejected,rejections=extract_claims(Path(case['pdf']),client,0,
                lambda level,message:events.append(dict(level=level,message=message)),packets=case['packets'],
                claims_per_packet=4,packet_results=packet_results,
                source_identifiers=case['references']['identifiers'],bibliography=case['references']['bibliography'])
            candidates,duplicates=deduplicate_source_claims(raw)
            result.update(status='completed' if all(p['status']=='completed' for p in packet_results) else 'partial',
                          candidates=candidates,canonical_duplicates=duplicates,rejected=rejected,rejections=rejections)
        except Exception as exc:
            result.update(status='failed',error=f'{type(exc).__name__}: {exc}',candidates=[])
        result.update(extraction_seconds=round(time.monotonic()-started,3),attempts=attempts,
                      extraction_metrics=client.request_metrics,packets=packet_results,events=events)
        observed=[a for a in attempts if a.get('usage_reported')]
        result['disabled_verified']=(arm=='disabled' and len(observed)==len(attempts) and bool(observed)
                                    and all(a.get('reasoning_usage_reported') and a['reasoning_tokens']==0 for a in observed))
        dump(folder/'extraction.json',result)
        source_attempts=[]
        def source_metrics(row):
            source_attempts.append(row)
            dump(folder/'source_attempts.json',source_attempts)
        source=OpenCodeClient(settings,task='source',on_request=source_metrics,response_dir=folder/'source_responses')
        shuffled=sorted(result['candidates'],key=lambda c:hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest())
        blinded=[]
        for c in shuffled:
            row=deepcopy(c)
            row['candidate_id']='audit-'+hashlib.sha256(json.dumps(c,sort_keys=True).encode()).hexdigest()
            blinded.append(row)
        try:
            judged=list(iter_batched_judgments(blinded,source,validate_source_claim,batch_size=3,label='Reasoning pilot source audit'))
            result['source_reviews']=[dict(original_candidate_id=original['candidate_id'],candidate=original,review=decision)
                                      for original,(_,decision) in zip(shuffled,judged,strict=True)]
        except Exception as exc:
            result['source_audit_error']=f'{type(exc).__name__}: {exc}'
            result['source_reviews']=[]
        result.update(source_attempts=source_attempts,source_metrics=source.request_metrics,finished_at=utc_now())
        dump(folder/'result.json',result)
        return dict(case=case['id'],arm=arm,status=result['status'],claims=len(result['candidates']),
                    reasoning=result['extraction_metrics']['reasoning_tokens'],final=result['extraction_metrics']['output_tokens'],
                    seconds=result['extraction_seconds'],disabled_verified=result['disabled_verified'],
                    source_decisions=[r['review']['decision'] for r in result['source_reviews']])

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures=[pool.submit(execute,c,arm) for c in cases for arm in ['provider_default','disabled']]
        for future in as_completed(futures):
            print(json.dumps(future.result()),flush=True)
    dump(report/'completed.json',dict(completed_at=utc_now()))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',type=Path,required=True)
    parser.add_argument('--prepared',type=Path,required=True)
    parser.add_argument('--targeted-followup',action='store_true')
    args=parser.parse_args()
    run(args.report,args.prepared,targeted=args.targeted_followup)
