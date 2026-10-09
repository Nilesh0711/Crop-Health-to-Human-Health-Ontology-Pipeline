# Extraction baseline

`extraction_baseline_2026-10-08.jsonl` preserves 105 legacy candidate summaries from five PDFs before rerunning the revised pipeline. Its companion `.summary.json` has the per-paper counts. This is a reproducible comparison set, **not** a labelled accuracy benchmark.

`extraction_baseline_qwen_sample_2026-10-08.jsonl` adds provisional Qwen source-evidence decisions for three eligible legacy claims. The full 105-claim model audit was intentionally not run; self-review cannot replace independent ground truth and would add substantial latency.

After a fresh PDF run, compare with:

```bash
./.venv/Scripts/python.exe scripts/extraction_audit.py compare --baseline analysis/extraction_baseline_2026-10-08.jsonl
```

The report gives counts and exact-triple overlap. A higher number of schema-valid claims is not evidence of higher precision. The original generated PDF outputs under `ontology/` were not modified by this snapshot.
