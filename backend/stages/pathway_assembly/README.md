# Pathway Assembly

After external validation, **MiMo Flash** aligns entity occurrences across
validated PDF claims in `ontology/`. Code connects the existing directed edges
through those shared entities. This is global graph assembly, without a causal
chain correctness/compatibility assessment. It does not add an A-to-C claim.

Assembly requires a PDF `entailed` decision, an explicit external `in_scope=true`
assessment, a completed independent-supported or corroborated result, and at
least one accepted independent supporting paper. Source review, insufficient
evidence, source-citation-only support, conflicts, failures and pending results
are excluded. Original claims and review reasons remain in document audit/source
views. Admission follows saved validation decisions; it does not infer scientific
truth from words or relation/type combinations.

Abstract example (placeholder names, not a scientific finding):

```text
Disease D --affects--> Crop C material
this affected material --has_contaminant (may)--> Compound T

               shared Crop C material entity
Disease D --> Crop C material --> Compound T
```

The model receives each occurrence's original triplet, surface mention, source
passage, local context, heading and qualifiers. It can align an explicit anaphor
or equivalent wording across documents; uncertainty in entity identity remains
separate. Infection conditions, modality, source/external states, raw entity
names, source pages and claim IDs remain attached to their original edges.

## Global strategy

1. Read structurally valid saved triplets from every document folder. Namespace
   claim references by document so identical candidate IDs in different PDFs
   cannot collide. Include only the validated claims described above.
2. Reuse unchanged entity-occurrence alignments from the last snapshot. New or
   changed occurrences enter the model in batches of 24 by default. Removed
   occurrences are pruned. Status-only updates refresh edges without new LLM calls.
3. Compare each incoming batch against **all global registry shards**. There is
   no top-k alias prefilter and no per-paper-only registry. Every older occurrence
   context is available in the scan; large node context lists split into fragments
   rather than silently losing passages. Provisional groups remain consistent
   across shards, and equivalent existing groups can merge.
4. Require every incoming endpoint ID exactly once. Reject invented/duplicated
   IDs and references to nodes absent from the request. These are data-integrity
   checks, not causal-chain validation. The model cannot create/reverse edges.
5. Rebuild the directed graph using all original edges and model-aligned node IDs.
   Enumerate simple multi-edge paths over those admitted edges without requiring
   that every path reach a human-health outcome.
   The UI paginates paths and defaults to six edges per displayed path. The
   complete assembled graph retains every eligible edge, including parallel
   occurrences and disconnected components.

Identity matching is a model judgment and can be wrong. Connecting entities does
not change any original scientific decision. This stage does not perform a
second external validation, infer net effects or convert connected findings into
a new direct causal claim.

## Runtime and persistence

- Route: `pathway_assembly` → `opencode-go/mimo-v2.6-flash`; primary only.
- Defaults: `PATHWAY_ASSEMBLY_BATCH_SIZE=24`,
  `PATHWAY_ASSEMBLY_INPUT_CHARACTERS=220000`. The latter is an application budget,
  not a claim about the provider's context limit. Oversized requests fail visibly.
- `ontology/.pathways/snapshot.json` atomically stores the complete global graph,
  canonical groups and occurrence ledger. Raw candidates and validations stay in
  their existing document folders.
- `checkpoint.json` records batch/shard progress. A failed scan never replaces
  a completed snapshot with a partial graph. Resume uses the same corpus signature;
  changed input starts from compatible unchanged alignments. MiMo errors use the
  existing bounded structured-response recovery and appear in Activity.
- `/api/pathways` is read-only and flags stale snapshots. It returns graph,
  paginated paths and assembly state. It never starts model calls.
- **Paths → Global pathway assembly → Assemble saved claims** queues a deliberate
  assembly-only job, without extraction or external revalidation. New full runs
  and successful external claim actions also run this stage automatically.
- The previous source/external/audit graphs and corroborated health-path view
  remain available. Assembly model changes do not invalidate external-validation
  signatures or completed extraction checkpoints.

Full global scans can still be expensive as a corpus grows. Batching, checkpoint
reuse and incremental occurrence updates limit repeated work without dropping
registry candidates. The fixed four-claim extraction cap is separately replaced
by `claims_per_packet=0` (all eligible claims); finite generation capacity and
possible extraction omissions still apply.
