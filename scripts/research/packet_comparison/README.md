# Extraction packet comparison

These developer tools compare the existing 4,000-word / 500-word-overlap layout
with 2,250-word packets that prefer paragraph boundaries, retain overlap and add
up to two original neighboring sentences within the same section and chapter.
They do not change the application's default or write `ontology/` artifacts.

Preparation parses each PDF once. Both layouts receive the same sentence stream.
Coverage means coverage of **parsed body sentences**, not proof that the PDF
parser recovered every table, page or original sentence correctly. Parser-detected
text blocks are not guaranteed to be editorial paragraphs. A long
paragraph may still split; the 400-word paragraph slack limits expansion, while
complete sentences and the two boundary neighbors can exceed the nominal target.

Run from the repository root with the project's Python environment. The compare
and audit commands make real OpenCode requests using the existing backend-only
credential. The prepare, replay and summarize commands do not call a model.

```powershell
python -m scripts.research.packet_comparison.prepare --report reports/my_packet_trial --pdf-root pdf --corpus scripts/research/packet_comparison/corpus.json --targets scripts/research/packet_comparison/reference_targets.json
python -m scripts.research.packet_comparison.compare --report reports/my_packet_trial --repeats 2 --workers 2
python -m scripts.research.packet_comparison.replay --report reports/my_packet_trial
python -m scripts.research.packet_comparison.audit --report reports/my_packet_trial --grouped
python -m scripts.research.packet_comparison.summarize --report reports/my_packet_trial
```

`corpus.json` identifies two complete articles and PDF pages 26–37 of the
soil/human-health book. `reference_targets.json` freezes 12 source-text targets,
their restrictions and a negative control before generation. They are assistant
source annotations, not independent expert gold. Custom corpora need their own
targets. Preparation rejects targets that do not match the parsed source.

The extraction comparison fixes the chosen model, prompt, four claim slots per
packet, 12,288-token initial generation allowance, provider-default reasoning and
the existing bounded same-model recovery. The seed randomizes scheduling, not
the provider's model generation. Every paper/repeat includes both layouts.
Smaller packets have more claim slots; a higher candidate count is not recall.

Requests save individual checkpoints, metrics and accepted provider responses.
Completed jobs are reused. If a process stops after starting a request but before
saving its result, the command refuses to repeat the uncertain paid request;
inspect its response artifacts first. Source PDFs and reference targets are
fingerprinted, and changing the frozen plan requires a fresh report directory.

Replay passes the saved answers through unmodified production extraction and
canonical deduplication, restoring cross-packet duplicate handling without new
generation. Missing provider responses stay missing. Audit checks all distinct
full candidate inputs with the existing source LLM and quotation/alignment
guards; it does not run external literature validation or the optional NLI model.
No layout label is given to the judge, but differences in claim wording and
context can reveal differences. Exact full-input caching preserves those differences.

`--watch` on audit can check raw candidates while extraction runs. This uses up
to two extraction requests plus one source request and may check variants later
merged by production deduplication. Prefer the sequential grouped workflow above
for a new study. Source-audit cost is separate from extraction cost.

Transport errors remain unknown; no verdict or evidence is invented. The
2026-10-24 study predeclared one additional **complete paired repeat** for a paper
affected by a transport failure, retaining the initial failures. `--first-repeat
3 --repeats 1` supports that separate supplement directory. A source audit with
`--remeasure-errors --grouped` measures only transport-failed audit inputs once,
in a separate directory; unsupported and needs-review answers are never retried
to seek a favorable verdict.

Compare paired complete paper/repeat results, qualifiers, boundary errors,
reference targets, recoveries, and tokens/time per source-entailed claim. Keep the
baseline if quality evidence is mixed. The model audit is a source-fidelity
proxy from the same model family as extraction. Expert blind annotation and a
larger held-out corpus are needed for research-grade precision/recall claims.
