# Provisional global Pathway Assembly

The active pipeline runs assembly **before external validation**. MiMo Flash
aligns source-grounded endpoint occurrences across saved documents, then code
connects the original directed edges. For `A -> B` and `B -> C`, assembly proposes
`A -> B -> C`; it does not add `A -> C` or establish causal transitivity.

## Admission and identity

`collect_corpus(..., provisional=True)` requires structural validity, effective
PDF fidelity `entailed` and explicit project scope `in_scope`. Valid manual
reviews contribute to those effective decisions without replacing the model's
original assessment. External support is not required to propose a chain.
Excluded and unresolved claims remain visible with their source evidence.
The admission policy is `source-entailed-in-scope-provisional-v2`.

Each occurrence retains its original edge, source mention, literal passage,
local context, heading, qualifiers, page and document-scoped claim identity.
The model can resolve an explicit referent or equivalent contextual wording;
category membership alone does not make two entities identical. Original
relation direction, modality and restrictions remain attached to each edge.

Runtime examples use abstract entities:

```text
Factor A --changes--> Process B
this process --changes--> Outcome C

Proposed route after context-grounded identity alignment:
Factor A -> Process B -> Outcome C
```

## Global alignment

1. Read eligible claims from all saved document folders and namespace their IDs.
2. Reuse unchanged occurrence mappings. Batch new/changed occurrences and compare
   them against retrieved global candidates from names, contextual embeddings,
   lexical signals and shared source context. Small cheap registries use complete
   scans. Selected-node context lists split rather than silently losing text.
3. Use short request-local E/N IDs and keyed scalar group assignments through
   `protocol.IdentityPacket`. Each incoming occurrence has one assignment; each
   registry entry is assigned once or explicitly unchanged.
   Groups precede assignments and declare whether they join an existing registry
   node. Code checks that boolean against actual membership; it does not interpret
   explanations or provisional type labels to choose a merge. Substance, process
   and affected-material states remain distinct unless actual referents coincide.
4. Check coverage, referenced identities and group continuity. Conflicting or
   invented assignments fail with bounded same-model recovery; code does not guess
   identity, discard conflicting members or rewrite an original relation.
5. Store the complete provisional graph. `neighbor_validation.targets` indexes incoming/outgoing adjacency and selects
   two-edge local pairs, including cross-document branches. Pair budgets are explicit;
   longer chains are display candidates rather than full-route assessment targets.

Exact repeated source text can be shared through references without summarizing
it. Identity alignment is a model judgement and remains fallible. Its result is
not external evidence or a numerical trust score.

## Persistence and controls

| Setting/artifact | Current behavior |
| --- | --- |
| Model | `opencode-go/mimo-v2.6-flash`, primary only |
| `PATHWAY_ASSEMBLY_BATCH_SIZE` | 24 by default; 1-64 |
| `PATHWAY_ASSEMBLY_INPUT_CHARACTERS` | 220,000 application characters by default; not a verified provider context limit |
| `ontology/.provisional_pathways/snapshot.json` | Complete active global identity registry and graph |
| `ontology/.provisional_pathways/checkpoint.json` | Batch/shard progress and compatible mappings |
| `ontology/.neighbor_validation/` | Local pair assessments and support projection |
| `ontology/.pathways/` | Historical externally supported assembly, retained for old-result readers |

A failed or stopped scan does not replace the last complete snapshot with a
partial graph. Changed inputs invalidate affected results; compatible unchanged
alignments can be reused. Full runs, continuation and source repair coordinate
assembly with pathway assessment through `backend/pipelines/`.

The v48 assembly version is 2; older mappings are rebuilt rather than seeded into
the new decisions. Explanation-first output is a hypothesis to benchmark, not a
proven fix for model reasoning. See [the update](../../../docs/protocols/identity_atomicity_and_qualified_support.md).

The explicit **Assemble saved claims** action runs assembly only. It does not
extract claims or grant external support. `/api/pathways?view=provisional` reads
candidate paths; the validated view returns eligible locally supported pairs
when neighboring-pair records exist. Historical whole-route results keep their
original assessment scope. GET requests never start models. The supported union
graph is not traversed to fabricate additional validated routes or imply a
complete-chain assessment.

Historical `provisional=False` readers retain the older independently-supported
claim policy. They are compatibility behavior, not a selectable live pipeline.
See [neighboring-claim validation](../../../docs/protocols/neighboring_claim_validation.md).

## Indexed selection (introduced in v45)

The default uses contextual endpoint candidates, retaining model-owned identity
and all source context for selected nodes. Claim_text reaches the model explicitly
through lossless text references. Unanchored names get a wider initial pool;
rejected narrow candidates can widen once. Complete small-registry scans avoid
unnecessary embedding warmup and extra model calls. Bounded candidate coverage is
saved and shown as a warning; an unmatched entity is not a verified global nonmatch.
See [configuration, coverage and verification](../../../docs/protocols/indexed_pathway_assembly.md).
