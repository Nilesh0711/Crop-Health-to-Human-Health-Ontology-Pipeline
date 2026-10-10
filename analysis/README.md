# Extraction baseline

`extraction_baseline_2026-10-08.jsonl` preserves 105 legacy candidate summaries from five PDFs before rerunning the revised pipeline. Its companion `.summary.json` has the per-paper counts. This is a reproducible comparison set, **not** a labelled accuracy benchmark.

`extraction_baseline_qwen_sample_2026-10-08.jsonl` adds provisional Qwen source-evidence decisions for three eligible legacy claims. The full 105-claim model audit was intentionally not run; self-review cannot replace independent ground truth and would add substantial latency.

After a fresh PDF run, compare with:

```bash
./.venv/Scripts/python.exe scripts/extraction_audit.py compare --baseline analysis/extraction_baseline_2026-10-08.jsonl
```

The report gives counts and exact-triple overlap. A higher number of controlled-vocabulary-valid claims is not evidence of higher precision or scientific correctness. The original generated PDF outputs under `ontology/` were not modified by this snapshot.

## Current-run evaluation

Every completed pipeline run creates a status-stratified
`ontology/<PDF stem>/evaluation_sample.jsonl`. Its `prediction` block is the
pipeline output and its `gold` block is intentionally blank so the extracting
model does not grade itself. After adding independent labels, calculate strict
triple/type/relation/source metrics and retrieval Recall@k/nDCG@k with:

```bash
./.venv/Scripts/python.exe -m analysis.evaluate_pipeline score "ontology/<PDF stem>/evaluation_sample.jsonl"
```

To rebuild a sample from an existing run:

```bash
./.venv/Scripts/python.exe -m analysis.evaluate_pipeline sample "ontology/<PDF stem>" --limit 80
```
