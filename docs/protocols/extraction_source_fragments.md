# Extraction source fragments (v36)

The Crop Health run `0a757b29fb1f46d2aa2ff6e056521801` stopped on target 9/15.
Both normal-finish responses selected sentence IDs `[69, 72]`. In the parsed
PDF, sentence 69 starts a statement on page 8; an intervening table occupies
page 9; sentence 72 continues it on page 10. The old consecutive-ID rule rejected
the entire response twice. Reasoning usage was zero and neither response was
truncated. Increasing generation budgets could not resolve the structural rule.

The extraction contract now permits 1–3 unique ascending original IDs inside
the assigned target. It does not expand a sparse selection, remove text from
the PDF or infer that two fragments form a scientifically supported finding.
Each selected fragment retains exact text, its sentence ID and PDF page.
Non-adjacent fragments have an explicit gap marker in the compatibility span;
every intervening sentence is preserved separately and passed to source fidelity.
The frontend displays fragments individually with the intervening text available.

The source model must assess continuity, omitted conditions, conflicts and direct
support. Each proof anchor must occur inside a real selected fragment; neither
gap metadata nor text spliced across a gap can supply a literal proof anchor.
This handles a PDF reading-order interruption; it does not repair the PDF
parser's table/heading segmentation or establish scientific correctness.

Each request's JSON schema lists its allowed sentence IDs and disallows repeated
IDs. Invalid IDs receive specific contract feedback and bounded regeneration
with the same budget. Genuine output truncation retains the existing increased
budget recovery. Models, Thinking settings and retry limits are unchanged.

Offline replay retained all eight proposals in the saved retry for source review.
The earlier response retained fourteen of fifteen, discarding one non-verbatim
ellipsis anchor. No live inference was used, and original run artifacts remain
unchanged. The interrupted run had completed eight targets with 87 in-memory
candidates; those are not a completed extraction checkpoint. Start a fresh run
after loading v36. Response artifacts remain available for inspection.
