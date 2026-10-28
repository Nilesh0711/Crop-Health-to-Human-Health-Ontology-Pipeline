"""Integrity-checked, immutable viewer snapshots. Uses only the standard library."""
import hashlib
import json
from pathlib import Path, PurePosixPath

FORMAT = 'research-viewer-v1'


def digest(data):
    return hashlib.sha256(data).hexdigest()


def manifest_id(manifest):
    return digest(json.dumps({k: v for k, v in manifest.items() if k != 'snapshot_id'},
                             sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode())[:24]


def safe_path(root, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts or '\\' in relative or not path.parts:
        raise ValueError('Invalid snapshot file path')
    result = (root / relative).resolve()
    if not result.is_relative_to(root.resolve()):
        raise ValueError('Snapshot file escapes its root')
    return result


def validate_graph(graph):
    nodes = [n['id'] for n in graph.get('nodes', [])]
    edges = [e['id'] for e in graph.get('edges', [])]
    if len(nodes) != len(set(nodes)) or len(edges) != len(set(edges)):
        raise ValueError('Duplicate graph identity')
    known = set(nodes)
    if any(e['source'] not in known or e['target'] not in known for e in graph.get('edges', [])):
        raise ValueError('Graph edge references a missing entity')


class Snapshot:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.manifest = None

    def load(self):
        path = self.root / 'manifest.json'
        if not path.is_file():
            raise ValueError(f'No viewer snapshot at {self.root}; run python -m backend.cli.export_viewer first')
        manifest = json.loads(path.read_text(encoding='utf-8'))
        if manifest.get('format') != FORMAT or manifest.get('snapshot_id') != manifest_id(manifest):
            raise ValueError('Invalid viewer manifest or snapshot identity')
        self.manifest = manifest
        for relative in manifest['files']:
            self.bytes(relative)
        documents = self.read(manifest['catalog'])
        ids = [d['id'] for d in documents]
        if len(ids) != len(set(ids)) or set(ids) != set(manifest['documents']):
            raise ValueError('Document catalogue does not match snapshot')
        jobs = {}
        for identifier, entry in manifest['documents'].items():
            claims = self.read(entry['claims'])['claims']
            claim_ids = [r['candidate']['candidate_id'] for r in claims]
            if len(claim_ids) != len(set(claim_ids)):
                raise ValueError('Duplicate candidate identity')
            for row in claims:
                validation = row.get('validation')
                if validation and validation.get('candidate_id') != row['candidate']['candidate_id']:
                    raise ValueError('Claim and validation identities differ')
            for key in ('graph', 'audit-graph', 'source-graph'):
                graph = self.read(entry[key]); validate_graph(graph)
                if any(e['id'] not in set(claim_ids) for e in graph['edges']):
                    raise ValueError('Document graph references a missing claim')
            for job_id, relative in entry['jobs'].items():
                job = self.read(relative)
                if job_id in jobs or job.get('id') != job_id or job.get('pdf_id') != identifier:
                    raise ValueError('Job belongs to a different document')
                jobs[job_id] = relative
        if jobs != manifest['jobs']:
            raise ValueError('Global job index does not match document archives')
        for document in documents:
            if document.get('latest_job_id') and document['latest_job_id'] not in manifest['documents'][document['id']]['jobs']:
                raise ValueError('Latest job is not in its document archive')
        for relative in manifest['graphs'].values():
            validate_graph(self.read(relative))
        for relative in manifest['pathways'].values():
            validate_graph(self.read(relative)['graph'])
        return self

    def bytes(self, relative):
        entry = self.manifest['files'].get(relative)
        if not entry:
            raise ValueError('File is not in the snapshot manifest')
        try:
            data = safe_path(self.root, relative).read_bytes()
        except OSError as exc:
            raise ValueError(f'Missing/unreadable snapshot file: {relative}') from exc
        if len(data) != entry['size'] or digest(data) != entry['sha256']:
            raise ValueError(f'Snapshot integrity failure: {relative}')
        return data

    def read(self, relative):
        return json.loads(self.bytes(relative))

    def document(self, identifier):
        return self.manifest['documents'].get(identifier)
