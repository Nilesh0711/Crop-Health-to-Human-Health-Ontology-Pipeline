# Frontend

The React/Vite/Cytoscape application supports the local research workspace and
the published read-only viewer. The current pipeline is v50 and validates neighboring claims and their local connections. See [the active protocol](../docs/protocols/neighboring_claim_validation.md).

Assessment cards separate scientific findings from execution completeness, show
partial wording support and contextual evidence, and display original/proposed
corrections with proof links. Proposals do not edit claims or admit a pending pair
to the supported graph. Older-contract findings remain visible as needing refresh.
Local cards distinguish both relationships having support with a compatible
junction but unresolved original details from single-component support. This is
a display breakdown, not a new support verdict. Mechanistic, contextual and
exclusion meanings come from Pro; exclusions cannot become positive supported
paths. Pending/stale breakdowns remain explicitly provisional/previous.

## Run and build

Run inside `frontend/`:

```bash
npm install
npm run dev
npm test
npm run build
```

Vite serves port 5173 and proxies `/api` to `http://127.0.0.1:8000`.
`dist/` contains generated production assets; do not edit or commit those files
or `node_modules/`. FastAPI serves a built frontend on port 8000. The isolated
results viewer can serve the same build on port 8011 for local preview.

## Ownership

| Location | Responsibility |
| --- | --- |
| `src/main.jsx` | React initialization and stylesheet imports |
| `src/App.jsx` | Page composition and view-level selection/filter state |
| `src/hooks/useWorkspaceNavigation.js` | Selected paper, URL view and saved sidebar preference |
| `src/hooks/useWorkspaceData.js` | Paper catalogue, capabilities and concurrent finding-view loading |
| `src/hooks/usePipelineRun.js` | Run settings, job polling, recovery, stop/resume/redo and source actions |
| `src/lib/api.js` | Shared API requests and status-preserving errors |
| `src/lib/workspaceData.js` | Fetch and normalize graph/claim/evidence views |
| `src/lib/jobPolling.js` | Non-overlapping polling and missing-job handling |
| `src/lib/claimPresentation.js` | Claim labels, grouping and search helpers |
| `src/components/claims/` | Claim cards, exact evidence, decisions, history and source controls |
| `src/components/graph/` | Stable assembled network, provisional paths, local-pair evidence and historical route views |
| `src/components/workspace/` | Library, navigation, run setup, summaries, recovery and activity |
| `src/components/diagnostics/` | Model usage, duration and request diagnostics |
| `src/styles/` | Consolidated light layout/theme in `base.css` and pathway styles |

The old `light.css` layer is merged into `base.css`. The existing workspace URLs
and sidebar storage key remain compatible. Named exports from `App.jsx` preserve
saved-view consumers while presentation helpers live in their owning modules.

## Research findings

**Network & pathways** replaces the separate Graph and Paths tabs. Older
`#graph` and `#paths` links open this unified view.

- The aligned candidate network sits beside a searchable, compact pathway list.
  Selecting a route highlights its exact edges without rearranging the map.
- Retired local contracts and connections removed from current assembly appear in
  **Earlier local assessments**, with original evidence, outside current counts.
  Still-current pairs needing reassessment remain visible. Slow older refreshes
  cannot replace newer completed findings; historical evidence links remain usable.
- Simple outcome filters separate locally supported pairs, parts supported, unassessed /
  review and excluded routes. Current local support requires a non-stale eligible pair result; historical whole-route results are separate.
- **View evidence** opens one local-pair assessment at a time: original source
  passages, exact paper-bound quotations, link/connection findings, queries,
  conditions and incomplete evidence remain available. Historical NLI fields remain readable.
- **Options** holds source/audit/supported data layers, relation-state filters,
  detailed outcomes, sort/depth settings, the saved GraphML export and connection
  rebuild. Rebuild explains that changed inputs can invalidate older assessments.
- The source-claim register, coverage explanation and historical research views
  are collapsed below the map. Standalone claims remain in the full candidate
  network; arbitrary paths through its union are not automatically validated.

Historical runs retain labelled trust, validated-assembly and health-path views.
Search and filters operate on loaded results and make no scientific decisions.
The layout stacks on narrow screens; read-only viewers retain observation tools.

## Run controls and review

Run settings default to target/context extraction, no numeric claim cap (`0`),
800 target words and Thinking Off for each assigned model. Whole-packet extraction
remains selectable. Neighbor-pair count and papers per pair are runtime controls; graph display depth is separate;
there is no trust-mode selector, shared-claim batch size or per-claim external retry.

Continue / Assess saved pathways reuses compatible saved work. Redo external
validation repeats selected neighboring-pair external assessments in a new checkpoint
namespace while keeping extraction and compatible source/manual decisions.
Stop preserves checkpoints; restart replaces the current run only after explicit
confirmation. Missing live jobs expose recovery instead of silently restarting.

Source fidelity and scope have separate manual Accept/Reject controls. Source
repair remains available for eligible claims. Manual decisions make no model calls;
repair may extract a corrected claim and reassess affected pathways.

The activity view retains researcher milestones, outcomes, warnings/errors and
stage filters. Request progress and the model-performance view retain detailed
usage/timing without duplicating every retrieval event in Activity.

## Published viewer and verification

`/api/health` supplies explicit capabilities; execution controls fail closed until
permitted. Viewer mode hides mutations and retains published claims, evidence,
paths, archived settings and logs. See [deployment](../docs/deployment/render_viewer.md).

The recorded v50 verification on 1 November 2026 passed 59 frontend checks,
covering polling/recovery, findings,
source controls, pathway evidence and read-only behavior. The production build
passes with separate `react` and `cytoscape` vendor chunks. These checks do not establish
scientific accuracy. Documentation/UI refactors require no extraction rerun.

## Stable assembled-network observation

The unified workspace defaults to the assembled candidate network, with globally
aligned entities and the selected paper's original edges. All papers shows the
complete workspace assembly. Selecting a cross-paper route includes its current
connected edges. Stale evidence cannot be highlighted as if it assessed a newer
network. Supported, source and audit graphs remain separate advanced data layers.
Relation labels and the entity colour key are available under Display; labels for
the selected route remain visible without filling the whole map with edge text.

Cytoscape is retained, with deterministic initial placement and one initial layout.
New data is reconciled by stable IDs; polling and filtering retain existing node
positions, selection, pan and zoom. Obsolete evidence fields are removed on updates.
Resizing resizes the canvas without fitting or relaying out the network. Organic/
Flow and Rearrange are explicit layout actions; Fit and zoom controls are retained.
Lock nodes prevents accidental dragging while keeping selection/panning available.
Positions are held for the mounted view; a full page reload or changing graph
datasets can initialize a new arrangement. No scientific result is changed by layout.

`graphNetwork.js` owns graph scoping/filtering and in-place synchronization;
`ResearchNetwork.jsx` owns the unified observation view and progressive evidence
disclosure; `researchNetwork.js` scopes/filters route records without inferring
scientific support. `GraphWorkspace.jsx` remains a compatibility component.
Headless Cytoscape and rendered-component tests cover update/branch/filter stability,
evidence freshness and the separation between provisional and supported networks.

The v49 UI adds an inspectable independent quotation audit, arithmetic assistance and full-text access attempts. Claim details offer deliberate atomic re-extraction for repairable claims, show parent/child history and exclude actions in the read-only viewer. Page quality reports distinguish column-coordinate extraction from plain-text fallback; neither establishes correct scientific reading order.

New default synthesis and audit requests use Muse Spark 1.3 Contributor; Assigned models and run descriptions display the configured model dynamically. See [provider access requirements](../docs/protocols/muse_external_model.md) before running.
