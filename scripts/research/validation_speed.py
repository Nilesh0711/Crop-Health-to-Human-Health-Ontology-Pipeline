"""Offline, reproducible cache/scheduling checks; never makes an LLM/API call.

python -m scripts.research.validation_speed --output reports/validation_speed_2026-10-27 \
    --requests "ontology/<paper>/llm_requests.jsonl"

These controlled/replayed timings are not a live provider or accuracy benchmark.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import heapq
import json
from pathlib import Path
import time
import uuid

from backend.shared.evidence_index import EvidenceIndex
from backend.stages.trust_graph.scheduling import parallel_papers


def replay(durations, workers):
    slots = [0.] * workers
    for seconds in durations:
        ready = heapq.heappop(slots)
        heapq.heappush(slots, ready + seconds)
    return round(max(slots, default=0), 3)


def controlled_schedule(streaming, workers):
    # Same six source identities and fixed results; no scientific selection.
    fetch_delays = [.01, .04, .08, .12, .16, .20]
    began, first = time.monotonic(), None
    def fetch(n):
        time.sleep(fetch_delays[n])
        return n
    def judge(n):
        time.sleep(.05)
        return {'paper_id': n, 'sentence_ids': [1, 3], 'quote': 'Exact stored source text.'}
    with ThreadPoolExecutor(max_workers=6) as fetching, ThreadPoolExecutor(max_workers=workers) as judging:
        downloads = [fetching.submit(fetch, n) for n in range(6)]
        if streaming:
            pending = {}
            while downloads or pending:
                for future in list(downloads):
                    if future.done():
                        downloads.remove(future)
                        pending[judging.submit(judge, future.result())] = None
                for future in list(pending):
                    if future.done():
                        pending[future] = future.result()
                        if first is None:
                            first = time.monotonic() - began
                if pending and all(v is not None for v in pending.values()) and not downloads:
                    results = list(pending.values())
                    break
                time.sleep(.001)
        else:
            ready = [f.result() for f in downloads]
            results = []
            for _, result in parallel_papers(ready, judge, workers):
                if first is None:
                    first = time.monotonic() - began
                results.append(result)
    return {'first_checked_seconds': round(first, 4), 'total_seconds': round(time.monotonic() - began, 4),
            'results': sorted(results, key=lambda r: r['paper_id'])}


def benchmark(output, requests=None):
    output.mkdir(parents=True, exist_ok=True)
    serial = controlled_schedule(False, 3)
    ready3, ready4 = controlled_schedule(True, 3), controlled_schedule(True, 4)
    assert serial['results'] == ready3['results'] == ready4['results']
    fetches = []
    def source():
        fetches.append(1)
        time.sleep(.02)  # Controlled network/parser stand-in, not a real source.
        return {'paragraphs': [{'text': 'Factor A affects Process B under Condition C.'}]}
    times, material = [], []
    source_key = 'controlled:exact-id:' + uuid.uuid4().hex
    for _ in range(2):
        index = EvidenceIndex(output / 'cache')
        began = time.monotonic()
        material.append(index.material(source_key, source))
        times.append(round(time.monotonic() - began, 6))
    assert material[0] == material[1]
    result = {'kind': 'offline_controlled_and_duration_replay_not_live_accuracy_or_provider_benchmark',
        'scheduling': {'barrier_3': serial, 'ready_3': ready3, 'ready_4': ready4},
        'persistent_index': {'cold_warm_seconds': times, 'fetch_count': len(fetches), 'identical_text': True},
        'default_paper_workers': 3,
        'limitations': ['Fixed synthetic fetch/judge delays; no provider queuing or rate limits.',
            'No live scientific quality/cost comparison or search-service benchmark.',
            'First-time papers still need fetching; expanded shared inputs can require reassessment.']}
    if requests:
        rows = [json.loads(line) for line in requests.read_text(encoding='utf-8').splitlines() if line.strip()]
        start = max((n for n, r in enumerate(rows) if r.get('request_id') == 1), default=0)
        jobs, groups = [], []
        for row in rows[start:]:
            label = row.get('task', '')
            if label.startswith('grouped paper external:') and row.get('purpose') == 'judgement' and row.get('status') == 'ok':
                jobs.append(float(row['elapsed_seconds']))
            elif label.startswith('grouped cross-paper synthesis:') and row.get('purpose') == 'judgement':
                if jobs:
                    groups.append({'successful_primary_papers': len(jobs), 'workers_3': replay(jobs, 3),
                        'workers_4': replay(jobs, 4), 'observed_synthesis': row.get('elapsed_seconds')})
                    jobs = []
        result['saved_duration_replay'] = {'source': str(requests), 'groups': groups,
            'limitation': 'All selected successful primary papers assumed ready together; excludes retrieval, recovery and cache reads. Not predicted total run time.'}
    (output / 'benchmark.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    return result


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--requests', type=Path)
    args = parser.parse_args()
    result = benchmark(args.output, args.requests)
    print(json.dumps({k: v for k, v in result.items() if k != 'scheduling'}, indent=2))
