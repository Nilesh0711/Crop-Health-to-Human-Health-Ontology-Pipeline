"""Produce reproducible tables and source-bound review records for the packet trial."""
from __future__ import annotations
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[3]))
from scripts.research.packet_comparison.audit import candidate_key
from scripts.research.packet_comparison.compare import dump


def normalized(text):
    return ' '.join(str(text).casefold().split())


def summarize(report):
    manifest=json.loads((report/'manifest.json').read_text(encoding='utf-8'))
    results=[json.loads(p.read_text(encoding='utf-8')) for p in (report/'extraction').glob('**/result.json')]
    results.sort(key=lambda r:(r['job']['case'],r['job']['arm'],r['job']['repeat'],r['job']['packet']))
    groups=defaultdict(list)
    for result in results:
        job=result['job']
        groups[(job['case'],job['arm'],job['repeat'])].append(result)
    reviews=[]
    summaries=[]
    for (case,arm,repeat),group in groups.items():
        candidates={}
        for result in group:
            for candidate in result['candidates']:
                candidates.setdefault(candidate['candidate_id'],candidate)
        replay=report/'grouped'/case/arm/f'repeat_{repeat}'/'result.json'
        if replay.exists():
            merged=json.loads(replay.read_text(encoding='utf-8'))
            if merged.get('packet_job_count')==len(group):
                candidates={c['candidate_id']:c for c in merged['candidates']}
        decisions=Counter()
        for candidate in candidates.values():
            key=candidate_key(candidate)
            checked=report/'source_audit'/f'{key}.json'
            review=json.loads(checked.read_text(encoding='utf-8'))['decision'] if checked.exists() else {'decision':'pending'}
            original_decision=review['decision']
            remeasurement=report/'source_audit_remeasurement'/f'{key}.json'
            if original_decision=='audit_error' and remeasurement.exists():
                review=json.loads(remeasurement.read_text(encoding='utf-8'))['decision']
            # A malformed claim cannot count as an accepted pipeline candidate.
            decision='schema_invalid' if candidate.get('schema_error') else review['decision']
            decisions[decision]+=1
            reviews.append(dict(case=case,arm=arm,repeat=repeat,audit_key=key,candidate=candidate,
                                source_review=review,pipeline_decision=decision,
                                initial_audit_decision=original_decision,
                                expert_review=None,expert_qualifier_errors=None,expert_context_errors=None))
        metrics={field:sum(r['metrics'].get(field,0) or 0 for r in group)
                 for field in ['requests','input_tokens','output_tokens','reasoning_tokens']}
        attempts=[a for r in group for a in r['attempts']]
        summaries.append(dict(case=case,arm=arm,repeat=repeat,packets_finished=len(group),
            packets_completed=sum(r['status']=='completed' for r in group),
            packets_failed=sum(r['status']!='completed' for r in group),
            provider_errors=sum(bool(r.get('error')) for r in group),
            recoveries=sum(len(r['attempts'])>1 for r in group),
            truncations=sum(a['status']=='incomplete_response' and 'truncat' in str(a.get('error','')).lower() for a in attempts),
            provider_input_usage_missing=sum(not a.get('input_tokens_reported') for a in attempts),
            packet_seconds=round(sum(r['elapsed_seconds'] for r in group),3),
            proposed_claims=sum(p.get('proposed_claims',0) for r in group for p in r['packets']),
            raw_accepted_claims=sum(len(r['candidates']) for r in group),unique_candidates=len(candidates),
            decisions=dict(decisions),**metrics))
    aggregate={}
    for row in summaries:
        target=aggregate.setdefault(row['arm'],dict(decisions=Counter()))
        target['decisions'].update(row['decisions'])
        for field,value in row.items():
            if field not in {'case','arm','repeat','decisions'}:
                target[field]=target.get(field,0)+value
    for row in aggregate.values():
        entailed=row['decisions']['entailed']
        assessed=sum(row['decisions'][key] for key in ['entailed','needs_review','unsupported'])
        row['source_entailed_fraction_of_assessed']=round(entailed/assessed,4) if assessed else None
        row['input_tokens_per_entailed_candidate']=round(row['input_tokens']/entailed,1) if entailed else None
        row['request_seconds_per_entailed_candidate']=round(row['packet_seconds']/entailed,1) if entailed else None
        row['packet_seconds']=round(row['packet_seconds'],3)
    targets=json.loads((report/'reference_targets.json').read_text(encoding='utf-8'))
    target_reviews=[]
    for target in targets:
        matching=[dict(arm=r['arm'],repeat=r['repeat'],audit_key=r['audit_key'],
                       subject=r['candidate']['subject'],relation=r['candidate']['relation'],object=r['candidate']['object'],
                       certainty=r['candidate']['certainty'],qualifiers=r['candidate']['qualifiers'],
                       claim_text=r['candidate']['claim_text'],source_review=r['source_review'])
                  for r in reviews if r['case']==target['case'] and
                  normalized(target['quote']) in normalized(r['candidate']['source_evidence_span'])]
        target_reviews.append(dict(**target,evidence_selected_by=matching,
            warning='Selecting this sentence is not proof that the target edge and restrictions were extracted correctly.',
            assistant_source_review=None,expert_review=None))
    dump(report/'summary.json',dict(extraction_results=len(results),planned_packets=len(manifest['experiment']['plan']),
                                  arms=aggregate,paired_groups=summaries))
    dump(report/'claim_review.json',reviews)
    dump(report/'reference_review.json',target_reviews)
    lines=['# Extraction packet comparison','',
        f"Completed packet jobs: {len(results)}/{len(manifest['experiment']['plan'])}.",'',
        'Both layouts use the same DeepSeek model, prompt, four claim slots per packet, 12,288-token initial generation allowance, provider-default reasoning, and bounded same-model recovery. The smaller layout has more packets and therefore more claim slots. Counts below are summed within each paper/repeat; they are not unique scientific facts across the entire corpus.','',
        '| Layout | Packet jobs | Failed jobs | Recoveries | Candidates | Source entailed | Needs review | Audit errors | Input tokens | Request seconds |',
        '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |']
    for arm,row in aggregate.items():
        d=row['decisions']
        lines.append(f"| {arm} | {row['packets_finished']} | {row['packets_failed']} | {row['recoveries']} | {row['unique_candidates']} | {d['entailed']} | {d['needs_review']} | {d['audit_error']} | {row['input_tokens']:,} | {row['packet_seconds']:.1f} |")
    lines+=['','Source judgements are the existing pipeline verifier, including its evidence guards. They are source-fidelity proxies from the same model family, not expert gold or external scientific validation. Transport/audit errors remain unknown. Provider token counts omit any attempt where usage was not returned. Request seconds are summed call durations, not end-to-end wall time; calls ran concurrently. No significance claim is made from correlated claims and only two repeats.','',
        'Review `claim_review.json` for every candidate, quote, local context, qualifiers and verdict. `reference_review.json` tracks the 12 predeclared source targets and the candidates selecting each target sentence; sentence selection alone does not establish correct edge coverage. Raw request metrics, accepted provider responses, preparation snapshots, and the immutable manifest are retained beside this report.','',
        '| Paper/excerpt | Repeat | Layout | Completed/finished | Source entailed | Needs review | Unknown audit | Input tokens |',
        '| --- | ---: | --- | ---: | ---: | ---: | ---: | ---: |']
    for row in summaries:
        d=row['decisions']
        lines.append(f"| {row['case']} | {row['repeat']} | {row['arm']} | {row['packets_completed']}/{row['packets_finished']} | {d.get('entailed',0)} | {d.get('needs_review',0)} | {d.get('audit_error',0)+d.get('pending',0)} | {row['input_tokens']:,} |")
    (report/'results.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print(json.dumps(dict(results=len(results),arms=aggregate),ensure_ascii=True))


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--report',required=True,type=Path)
    summarize(parser.parse_args().report)
