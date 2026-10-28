"""Local-only snapshot export, reusing the existing scientific display contracts.

The deployed viewer does not import this module. Export never calls health,
job-recovery, repair, run or assembly actions. The original ontology is read only.
"""
from contextlib import contextmanager
from datetime import datetime, timezone
import importlib
import json
from pathlib import Path
import re
import tempfile
import uuid

from backend.viewer.snapshot import FORMAT, Snapshot, digest, manifest_id

ACTIVE = {'queued', 'running', 'stopping'}
SECRET_KEYS = {'api_key', 'opencode_api_key', 'authorization', 'access_token', 'pdf_path'}


def clean(value):
    if isinstance(value, dict):
        return {k: clean(v) for k, v in value.items() if k.casefold() not in SECRET_KEYS}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, str):
        return re.sub(r'\boc_sk_[A-Za-z0-9_-]+', '[redacted credential]', value)
    return value


def source_inventory(ontology, pdf_dir):
    paths = []
    for folder in ontology.iterdir():
        if not folder.is_dir() or folder.name == '.evidence_index':
            continue
        paths.extend(p for p in folder.iterdir() if p.is_file() and p.suffix in {'.json', '.jsonl', '.graphml'})
        if (folder / 'jobs').is_dir():
            paths.extend((folder / 'jobs').glob('*.json'))
    if pdf_dir.is_dir():
        paths.extend(pdf_dir.glob('*.pdf'))
    return {str(p.resolve()): digest(p.read_bytes()) for p in sorted(paths)}


def check_source_files(inventory):
    for name in inventory:
        path = Path(name)
        if path.suffix == '.json':
            json.loads(path.read_text(encoding='utf-8'))
        elif path.suffix == '.jsonl':
            for line in path.read_text(encoding='utf-8').splitlines():
                if line.strip() and not isinstance(json.loads(line), dict):
                    raise ValueError(f'Expected a JSONL object in {path.name}')


@contextmanager
def display_api(ontology, pdf_dir, registry):
    api = importlib.import_module('backend.api.app')
    previous = api.ONTOLOGY_DIR, api.PDF_DIR, api._pdf_map
    api.ONTOLOGY_DIR, api.PDF_DIR, api._pdf_map = ontology, pdf_dir, lambda: registry
    try:
        yield api
    finally:
        api.ONTOLOGY_DIR, api.PDF_DIR, api._pdf_map = previous


def export_snapshot(ontology, pdf_dir, output, *, include_pdfs=False, replace=False):
    ontology, pdf_dir, output = Path(ontology).resolve(), Path(pdf_dir).resolve(), Path(output).resolve()
    if not ontology.is_dir():
        raise ValueError('Ontology directory does not exist')
    if output in {ontology, pdf_dir} or output.is_relative_to(ontology) or output.is_relative_to(pdf_dir) or ontology.is_relative_to(output) or pdf_dir.is_relative_to(output):
        raise ValueError('Snapshot destination must be separate from source data')
    if output.exists():
        if not replace:
            raise ValueError('Snapshot already exists; use --replace to publish a new validated version')
        Snapshot(output).load()  # Never replace an unrelated directory.
    before = source_inventory(ontology, pdf_dir)
    check_source_files(before)
    folders = sorted(p for p in ontology.iterdir() if p.is_dir() and not p.name.startswith('.')
                     and any((p / n).exists() for n in ('candidates.jsonl', 'summary.json', 'run_state.json', 'document_scope.json')))
    if not folders:
        raise ValueError('No saved document runs to publish')
    from backend.shared.storage import stable_hash
    registry = {stable_hash(p.name + '.pdf', 16): pdf_dir / (p.name + '.pdf') for p in folders}
    output.parent.mkdir(parents=True, exist_ok=True)
    staging = Path(tempfile.mkdtemp(prefix='.snapshot-building-', dir=output.parent))
    manifest = {'format': FORMAT, 'created_at': datetime.now(timezone.utc).isoformat(),
                'files': {}, 'documents': {}, 'jobs': {}, 'graphs': {}, 'pathways': {}, 'warnings': []}

    def write(relative, value, binary=False):
        data = value if binary else json.dumps(clean(value), ensure_ascii=False, separators=(',', ':'), allow_nan=False).encode()
        path = staging / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        manifest['files'][relative] = {'size': len(data), 'sha256': digest(data)}
        return relative

    try:
        with display_api(ontology, pdf_dir, registry) as api:
            from backend.config.settings import Settings, SCHEMA_VERSION
            settings = Settings()
            manifest['pipeline'] = {'schema_version': SCHEMA_VERSION, 'task_models': settings.task_models,
                                    'external_phase_models': settings.external_phase_models,
                                    'knowledge_building_mode': settings.knowledge_building_mode}
            catalog = []
            for identifier, pdf in registry.items():
                folder = ontology / pdf.stem
                state = api._read_json(folder / 'run_state.json', {})
                if state.get('status') in ACTIVE:
                    raise ValueError(f'{pdf.stem}: latest run is marked active; stop/finish it locally before publishing')
                summary = api._summary_with_coverage(folder)
                status = state.get('status') or (summary or {}).get('run_status', 'not_started')
                item = {'id': identifier, 'name': pdf.name, 'title': pdf.stem,
                        'size_bytes': pdf.stat().st_size if pdf.is_file() else 0,
                        'status': status, 'summary': summary,
                        'document_scope': api._read_json(folder / 'document_scope.json'),
                        'can_resume': False, 'latest_job_id': state.get('id'), 'active_job_id': None,
                        'pdf_available': bool(include_pdfs and pdf.is_file()), 'snapshot_only': True}
                catalog.append(item)
                entry = {'jobs': {}}
                base = f'documents/{identifier}'
                for key, method in [('claims', api.pdf_claims), ('logs', api.pdf_logs),
                                    ('graph', api.pdf_graph), ('audit-graph', api.pdf_audit_graph),
                                    ('source-graph', api.pdf_source_graph), ('paths', api.pdf_paths), ('trust', api.pdf_trust)]:
                    payload = method(identifier)
                    if key == 'claims':
                        for row in payload['claims']:
                            row['repair_eligibility'] = {'allowed': False, 'reason': 'Read-only publication'}
                        candidates = {r['candidate']['candidate_id'] for r in payload['claims']}
                        for result in api.read_jsonl(folder / 'validation_runs.jsonl'):
                            if result.get('candidate_id') not in candidates:
                                raise ValueError('Validation record refers to a missing candidate')
                    entry[key] = write(f'{base}/{key}.json', payload)
                for path in sorted((folder / 'jobs').glob('*.json')):
                    record = json.loads(path.read_text(encoding='utf-8'))
                    job_id = record.get('id')
                    if not isinstance(job_id, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', job_id):
                        raise ValueError('Invalid saved job identifier')
                    if record.get('pdf_id') != identifier or job_id in manifest['jobs']:
                        raise ValueError('Saved job identity collision or document mismatch')
                    job = api._job_snapshot(record)
                    job.update(snapshot_only=True, recorded_status=job.get('status'))
                    if job.get('status') in ACTIVE:
                        job['status'] = 'snapshot'
                    if 'request_progress' in job:
                        job['recorded_request_progress'] = job.pop('request_progress')
                    job.pop('active_request_progress', None)
                    relative = write(f'{base}/jobs/{job_id}.json', job)
                    entry['jobs'][job_id] = manifest['jobs'][job_id] = relative
                if item['latest_job_id'] and item['latest_job_id'] not in entry['jobs']:
                    manifest['warnings'].append(f'{pdf.stem}: latest job archive is missing; document activity remains available')
                    item['latest_job_id'] = None
                if include_pdfs and pdf.is_file():
                    entry['pdf'] = write(f'{base}/source.pdf', pdf.read_bytes(), True)
                if (folder / 'knowledge_graph.graphml').is_file():
                    entry['graphml'] = write(f'{base}/knowledge_graph.graphml', (folder / 'knowledge_graph.graphml').read_bytes(), True)
                manifest['documents'][identifier] = entry
            manifest['catalog'] = write('catalog.json', catalog)
            for key, method in [('graph', api.master_graph), ('audit-graph', api.master_audit_graph), ('source-graph', api.master_source_graph)]:
                manifest['graphs'][key] = write(f'global/{key}.json', method())
            for view in ('validated', 'provisional'):
                payload = api.global_pathways(limit=1, max_hops=2, offset=0, view=view)
                for key in ('paths', 'has_more', 'offset', 'limit', 'max_hops'):
                    payload.pop(key, None)
                manifest['pathways'][view] = write(f'global/pathways-{view}.json', payload)
        if source_inventory(ontology, pdf_dir) != before:
            raise ValueError('Source files changed during export; finish local work and retry')
        manifest['snapshot_id'] = manifest_id(manifest)
        (staging / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding='utf-8')
        Snapshot(staging).load()
        backup = None
        if output.exists():
            backup = output.with_name('.snapshot-previous-' + uuid.uuid4().hex)
            output.rename(backup)
        try:
            staging.rename(output)
        except Exception:
            if backup:
                backup.rename(output)
            raise
        return {'snapshot_id': manifest['snapshot_id'], 'documents': len(catalog), 'files': len(manifest['files']),
                'bytes': sum(row['size'] for row in manifest['files'].values()),
                'output': str(output), 'previous_snapshot': str(backup) if backup else None,
                'warnings': manifest['warnings']}
    except Exception:
        # Keep failed staging for inspection; never touch the previous publication.
        raise
