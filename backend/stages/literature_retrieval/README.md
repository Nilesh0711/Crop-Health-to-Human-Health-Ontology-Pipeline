# Literature retrieval

The active neighboring-pair validator supplies queries for two adjacent claims
and their local connection conditions. MiMo Flash plans the link and junction
queries; it does not generate searches for an entire upstream/downstream chain.
Search and citation lookup use Europe PMC, OpenAlex
and Crossref. Resolved citations stay distinguishable from independent discovery.
Metadata ranks retrieval candidates; it does not establish scientific truth.

Fetching and chunk preparation use bounded `RETRIEVAL_WORKERS` (default 4).
Workers hand prepared documents to one ranking owner, so local model execution
has a single owner and returned evidence keeps deterministic selection order.
Selected text retains paper identity, section context and sentence numbering.

Claim-independent parsed papers and chunks share a bounded run cache and optional
persistent evidence index. The pathway validator reranks reused text against its
current targets; retained assessment records require compatible input signatures.
Search deadlines preserve unfinished sources as pending rather than treating
them as successful searches with no evidence. Missing or failed evidence stays
visible and resumable. Selected proof persists independently of cache expiry.

Retired per-claim prefetch/group-ready flags are no longer live settings. Their
research implementations are isolated under `archive/legacy_pipeline/research_historical/`.

## Workflow ownership and retained provenance

`workflow.retrieve_round` coordinates `_search_index`, `_merge_metadata`,
`_accessible_results`, `_load_material` and `_prepare_material`. A prepared paper
can enter judgement while other downloads continue. Final paper allocation/order
remains deterministic despite completion order.

Reference strings come from the originating PDF's parsed bibliography and its
sentence-level citation links. DOI/Crossref resolution maps those strings to an
actual publication; parser links are not proof that the external paper was fetched.
The citation ledger records identified/resolved/fetched/pending/unavailable states
and document identities. All originating PDFs in a route are excluded from external
corroboration. Resolved source citations receive additional slots outside the search
paper budget. A public-PDF lead can remain eligible without an abstract; a metadata-only
record without accessible evidence cannot become scientific proof.

The default independent-search budget is eight papers per neighboring pair,
with four retained chunks per paper. Link and local-connection rankings balance allocation;
[ranking](../evidence_ranking/README.md) determines current passage selection.
One bounded follow-up round can search specifically for missing local evidence,
including wording/measurement gaps and proposed corrections. Its default extra
discovered-paper budget is three. No pending facets means no follow-up planning call.
No Brave/Perplexity or generated-search-answer service is an active evidence provider.

## Citation and access diagnostics (v49)

Bibliography resolution separates missing text, malformed DOI, no candidates, ambiguous matches and title/author/year mismatches. Transient lookup errors remain pending. All existing XML routes and up to four distinct indexed public PDF copies are tried before abstract fallback. Evidence records retain access attempts. No new third-party search service is added. See [v49](../../../docs/protocols/proof_audit_atomic_reextraction_and_layout.md).
