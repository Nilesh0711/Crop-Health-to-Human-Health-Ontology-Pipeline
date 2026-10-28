# Frontend

The same application supports the local pipeline and the published read-only
viewer. `/api/health` supplies explicit capabilities; execution controls fail
closed until the server permits them. Viewer mode preserves result inspection
and archived settings/logs. See [deployment](../docs/deployment/render_viewer.md).

React/Vite/Cytoscape research workbench. Run these commands in this directory:

```bash
npm install
npm run dev
npm test
npm run build
```

`src/main.jsx` loads React and shared CSS. `src/App.jsx` owns application state,
API polling and page composition. Reusable views are grouped by responsibility:

- `src/components/claims/`: provenance, decision trace, citation tracking and repair.
- `src/components/graph/`: graph display, filters, layout, export controls and
  global Pathway Assembly. Paths retains the corroborated health-path view.
- `src/components/diagnostics/`: model timing, usage and performance diagnostics.
- `src/components/workspace/`: library/sidebar, navigation, run settings, activity,
  request progress and page-quality reporting.
- `src/styles/`: shared base CSS, the minimal light theme and scoped pathway styles.
- `tests/details.test.mjs`: rendering and feature-preservation checks, run by `npm test`.

The development proxy continues to send `/api` to `http://127.0.0.1:8000`.
Production assets are generated into `dist/`; do not edit or version generated
assets or `node_modules/`. Existing sidebar preferences and workspace URLs retain
their storage keys and routes.

Run settings default to target/context extraction and `0` claims per call
(all eligible claims). A positive value supplies an optional cap. **Paths →
Global pathway assembly → Assemble saved claims** queues an assembly-only job
using MiMo Flash; source passages, conditions and evidence states remain visible.

## Research pathways

Three view cards retain Trust network, Global pathway assembly and Corroborated
health paths. The global views show source-claim/entity/paper counts and loaded
path counts. Pathways, Network claims and Connection assessments are separate
panes; standalone claims remain accessible without forming a multi-edge path.

`PathwayCards.jsx` shows readable entity chains and expandable original claims,
conditions, source pages, identifiers and verbatim quotations. Selecting a
relation opens its evidence ledger. Search covers entities, relations, papers,
claim IDs and conditions. Evidence-state filters use saved assessments and match
at least one edge in a path; they do not classify the entire chain. Filters apply
to loaded paths, with explicit pagination. Graph controls, claim sorting, depth
selection and validated-only assembly remain available.

`src/lib/pathways.js` owns presentation/search helpers, not scientific decisions.
`tests/pathways.test.mjs` checks filtering, retained evidence and view controls.
These frontend changes require no extraction or validation rerun.
