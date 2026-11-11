# GitHub Pages Static Deployment

This deployment serves a **100% static read-only build** of the ontology workbench (`frontend/dist/` + pre-exported JSON/PDF data under `deploy/pages/data/`) with zero cold starts and no Python runtime needed in production.

## 1. Export or update static data

After finishing local pipeline runs, run from the repository root:

```powershell
.venv\Scripts\python.exe -m backend.cli.export_static --replace
```

Pass `--include-pdfs` if you want to bundle source PDFs inside `deploy/pages/data/` (kept off by default to keep the static payload ~`2 MB`).

## 2. Build & preview locally

```powershell
cd frontend
npm run build:pages
npx vite preview --host 127.0.0.1 --port 4173
```

Then open `http://127.0.0.1:4173/Crop-Health-to-Human-Health-Ontology-Pipeline/`.
For root-path preview (`http://127.0.0.1:4173/`), set `VITE_BASE_PATH=/` before running `npm run build:pages`.

## 3. Enable & deploy on GitHub Pages

1. Commit and push your code along with `deploy/pages/data/` and `.github/workflows/pages.yml` to `main`.
2. In your GitHub repository: **Settings → Pages → Build and deployment → Source**, choose **GitHub Actions**.
3. The **Deploy static viewer to GitHub Pages** workflow runs automatically on pushes touching `frontend/` or `deploy/pages/data/`, or manually from the **Actions** tab.
