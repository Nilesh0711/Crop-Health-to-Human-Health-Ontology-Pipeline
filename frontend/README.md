# Frontend

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
- `src/styles/`: shared base CSS and the minimal light theme.
- `tests/details.test.mjs`: rendering and feature-preservation checks, run by `npm test`.

The development proxy continues to send `/api` to `http://127.0.0.1:8000`.
Production assets are generated into `dist/`; do not edit or version generated
assets or `node_modules/`. Existing sidebar preferences and workspace URLs retain
their storage keys and routes.

Run settings default to target/context extraction and `0` claims per call
(all eligible claims). A positive value supplies an optional cap. **Paths →
Global pathway assembly → Assemble saved claims** queues an assembly-only job
using MiMo Flash; source passages, conditions and evidence states remain visible.
