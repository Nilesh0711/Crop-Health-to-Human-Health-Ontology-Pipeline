# Publish saved research results on Render Free

The existing React frontend supports both the local research workspace and a
published viewer. A separate FastAPI entry point, `backend.viewer.app:app`, serves
an immutable snapshot. It does not import inference providers, retrieval models
or pipeline orchestration. No API keys, database, GPU, or separate frontend site
are needed for observation.

The viewer retains claims, conditions, exact evidence, citation tracking,
validation decisions, revision history, request metrics, PDF-quality diagnostics,
activity filters, all three graph views, GraphML export, and validated/provisional
pathway browsing with depth and pagination controls. Run, resume, stop, repair,
retry, upload and assembly actions are unavailable. The server rejects write
requests with HTTP 403 even if a visitor bypasses the interface.

## Prepare or update the publication

Finish or stop local work first. From the project root:

```powershell
.venv/Scripts/python.exe -m backend.cli.export_viewer --include-pdfs
```

An initial snapshot is already prepared in `deploy/render/snapshot/`. To replace
it after completing more local runs, use:

```powershell
.venv/Scripts/python.exe -m backend.cli.export_viewer --include-pdfs --replace
```

Omit `--include-pdfs` to publish findings without original PDF downloads. The
button is then hidden. Only PDFs associated with saved runs are included.

The exporter reuses the local API's result formatting without running models or
calling its job-recovery endpoints. It exports every saved document and job,
compares source-file hashes before and after export to detect concurrent changes,
checks identifiers and graph links, and records SHA-256 hashes in a manifest.
Startup rejects missing or changed files. These checks detect accidental
corruption; they are not a digital signature or a scientific correctness check.

Replacement first builds and checks a sibling staging directory. The prior
publication is retained locally as `deploy/render/.snapshot-previous-*`. Failed
staging directories are also retained for inspection. Both are ignored by Git
and excluded from the deployment image. The original `ontology/` is not rewritten.
Keep local scientific outputs and their backups as the research record.

The snapshot contains saved research text and logs intended for public viewing.
The exporter removes credential fields and OpenCode key strings; it excludes raw
provider-response directories, embedding caches, `.env` files and local PDF paths.
Published data can be downloaded by visitors. Publication is separate from the
original private workspace.

## Preview locally

Build the shared frontend, then start the viewer on a different port from the
local pipeline:

```powershell
cd frontend
npm ci
npm run build
cd ..
.venv/Scripts/python.exe -m uvicorn backend.viewer.app:app --host 127.0.0.1 --port 8011
```

Open `http://127.0.0.1:8011`. The header identifies the results viewer and the
snapshot date and identifier. In Run details or Activity log, select an archived
job to inspect its own saved settings and log. Selecting a historical job does
not reconstruct historical claims: claims and graphs remain the published
document's current results. Old jobs saved while active are displayed as frozen
snapshots, retaining their original recorded status without polling or recovery.

## Deploy

1. Commit and push the implementation **including `deploy/render/snapshot/`**
   to the GitHub/GitLab repository you will connect to Render. `ontology/` remains
   local and ignored by Git. No implementation commit or push is done automatically.
2. Sign into [Render](https://dashboard.render.com/). Choose **New → Blueprint**,
   connect that repository and branch, and use the root `render.yaml`.
3. Check the generated service is **Free**. The blueprint creates one Docker web
   service and no databases or paid disks. Its build context is the repository
   root and Dockerfile is `deploy/render/Dockerfile`.
4. Deploy. The Docker build compiles the existing React app and installs only
   `deploy/render/requirements.txt` in the Python runtime. No model environment
   variables are required. The application listens on Render's `PORT`.
5. Verify `/api/health` returns `mode: viewer`, open the web page, and share the
   resulting `https://…onrender.com` URL with your guide.

Alternatively create **New → Web Service**, connect the repository, select
Docker, leave Root Directory empty, set Dockerfile Path to
`deploy/render/Dockerfile`, choose Free, and set health check `/api/health`.
No separate build/start command is necessary: the Dockerfile provides both.

The blueprint disables automatic redeployments. After exporting a replacement
and pushing it, choose **Manual Deploy → Deploy latest commit** on the service.
The local pipeline still starts through its existing scripts and entry point.

Render Free sleeps after 15 minutes without traffic and takes about one minute
to wake. Free runtime filesystem changes disappear on restart/redeploy; our
snapshot survives because it is part of the built image and Git source. A SQLite
file written on that runtime would have the same persistence limitation. Free
usage limits still apply; see [Render's Free documentation](https://render.com/docs/free).
The configuration follows [Render's Blueprint specification](https://render.com/docs/blueprint-spec).

## What is appropriate at this scale

For the current low-traffic project, frozen JSON and file hashes avoid a database
migration and keep each publication reproducible. The current export contains
one document, 49 claims and 11 archived jobs, approximately 15 MB with its PDF.
Exporting additional documents does not require rebuilding the UI. Thousands of
documents would eventually justify paginated catalogues/claims and external
artifact storage; this free single-service setup has not been benchmarked for
that scale.

## Verification

```powershell
.venv/Scripts/python.exe -m pytest tests/viewer tests/project/test_architecture.py -q
cd frontend
npm test
npm run build
```

Tests exercise snapshot integrity, source immutability, archive preservation,
read-only routes, unavailable PDFs, replacement behavior, concurrent-source
changes, and an import check excluding inference dependencies. Docker must be
installed to additionally build the deployment image locally:

```powershell
docker build -f deploy/render/Dockerfile -t research-results-viewer .
docker run --rm -p 8011:8000 research-results-viewer
```

Implementation verification on 28 October 2026: 680 Python tests, 20 frontend
tests and the production build passed. Every published claim rendered expanded
with its evidence in the rendering test. A live server using a clean environment
with only the viewer requirements matched 16 local result views, all 11 job
archives, and the original PDF; source hashes remained unchanged and execution
requests returned 403. The Docker image was not built locally because Docker was
unavailable. A live browser inspection was also unavailable in this environment.
