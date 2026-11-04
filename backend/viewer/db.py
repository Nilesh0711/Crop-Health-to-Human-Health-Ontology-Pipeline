"""SQLite-backed publication database for fast, low-memory viewer queries.

Uses only the Python standard library (sqlite3, json, pathlib).
Keeps Python process memory under 40 MB on constrained hosts like Render Free.
"""
from contextlib import contextmanager
import json
from pathlib import Path
import sqlite3
from typing import Any, Dict, List, Optional, Tuple

DATABASE_FILENAME = 'publication.db'
SCHEMA_VERSION = 1


def init_db_schema(conn: sqlite3.Connection):
    """Creates the publication database schema and indexes."""
    cur = conn.cursor()
    cur.execute("PRAGMA page_size = 4096;")
    cur.execute("PRAGMA synchronous = OFF;")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS metadata (
        key TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS documents (
        id TEXT PRIMARY KEY,
        name TEXT,
        title TEXT,
        status TEXT,
        summary_json TEXT,
        scope_json TEXT,
        can_resume INTEGER DEFAULT 0,
        latest_job_id TEXT,
        active_job_id TEXT,
        pdf_available INTEGER DEFAULT 0,
        data_json TEXT NOT NULL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS claims (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        candidate_id TEXT,
        subject TEXT,
        relation TEXT,
        object TEXT,
        verdict TEXT,
        data_json TEXT NOT NULL
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_claims_doc ON claims(document_id);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_claims_cand ON claims(candidate_id);")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pathways (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        pathway_id TEXT NOT NULL UNIQUE,
        view TEXT NOT NULL,
        eligible INTEGER DEFAULT 0,
        historical_eligible INTEGER DEFAULT 0,
        stale INTEGER DEFAULT 0,
        hops INTEGER DEFAULT 0,
        verdict TEXT,
        scientific_verdict TEXT,
        data_json TEXT NOT NULL
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pathways_query ON pathways(view, eligible, stale, hops);")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pathways_pid ON pathways(pathway_id);")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS pathway_documents (
        pathway_id TEXT NOT NULL,
        document_id TEXT NOT NULL,
        PRIMARY KEY (pathway_id, document_id)
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_pd_doc ON pathway_documents(document_id);")

    cur.execute("""
    CREATE TABLE IF NOT EXISTS document_views (
        document_id TEXT NOT NULL,
        kind TEXT NOT NULL,
        data_json TEXT NOT NULL,
        PRIMARY KEY (document_id, kind)
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS global_views (
        kind TEXT PRIMARY KEY,
        data_json TEXT NOT NULL
    );
    """)

    cur.execute("""
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY,
        document_id TEXT NOT NULL,
        status TEXT,
        data_json TEXT NOT NULL
    );
    """)
    cur.execute("CREATE INDEX IF NOT EXISTS idx_jobs_doc ON jobs(document_id);")

    conn.commit()


def truncate_context_packet(claim: dict, max_chars: int = 500) -> dict:
    """Trim repetitive uncollapsed section dumps from claim candidates for compact storage."""
    candidate = claim.get('candidate')
    if isinstance(candidate, dict):
        packet = candidate.get('source_context_packet')
        if isinstance(packet, str) and len(packet) > max_chars:
            candidate['source_context_packet'] = packet[:max_chars]
    return claim


def build_publication_db(snapshot_dir: Path | str, db_path: Optional[Path | str] = None) -> Path:
    """Compiles snapshot JSON files into a single, indexed, lightweight SQLite database."""
    snapshot_dir = Path(snapshot_dir).resolve()
    db_file = Path(db_path or (snapshot_dir / DATABASE_FILENAME)).resolve()

    if db_file.exists():
        db_file.unlink()

    conn = sqlite3.connect(db_file)
    init_db_schema(conn)
    cur = conn.cursor()

    # 1. Metadata
    manifest_path = snapshot_dir / 'manifest.json'
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        for k, v in manifest.items():
            if k not in {'files', 'documents', 'jobs'}:
                cur.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)",
                            (k, json.dumps(v, ensure_ascii=False, separators=(',', ':'))))

    # 2. Catalog & Documents
    catalog_path = snapshot_dir / 'catalog.json'
    title_to_id = {}
    if catalog_path.is_file():
        catalog_raw = catalog_path.read_text(encoding='utf-8')
        cur.execute("INSERT OR REPLACE INTO metadata VALUES (?, ?)", ('catalog', catalog_raw))
        catalog = json.loads(catalog_raw)
        for doc in catalog:
            doc_id = doc['id']
            title_to_id[doc.get('title')] = doc_id
            cur.execute("""
            INSERT OR REPLACE INTO documents VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                doc_id,
                doc.get('name'),
                doc.get('title'),
                doc.get('status'),
                json.dumps(doc.get('summary'), ensure_ascii=False, separators=(',', ':')),
                json.dumps(doc.get('document_scope'), ensure_ascii=False, separators=(',', ':')),
                1 if doc.get('can_resume') else 0,
                doc.get('latest_job_id'),
                doc.get('active_job_id'),
                1 if doc.get('pdf_available') else 0,
                json.dumps(doc, ensure_ascii=False, separators=(',', ':'))
            ))

    # 3. Global Views & Pathways
    global_dir = snapshot_dir / 'global'
    if global_dir.is_dir():
        for g in global_dir.glob('*.json'):
            kind = g.stem
            raw_text = g.read_text(encoding='utf-8')
            if kind == 'pathway-assessments':
                p_data = json.loads(raw_text)
                summary = {k: v for k, v in p_data.items() if k != 'records'}
                cur.execute("INSERT OR REPLACE INTO global_views VALUES (?, ?)",
                            ('pathway-assessments-meta', json.dumps(summary, ensure_ascii=False, separators=(',', ':'))))
                for r in p_data.get('records', []):
                    pid = r.get('pathway_id', '')
                    elig = 1 if r.get('eligible') else 0
                    h_elig = 1 if r.get('historical_eligible') else 0
                    stale = 1 if r.get('stale') else 0
                    hops = len(r.get('target', {}).get('edges', []))
                    cur.execute("""
                    INSERT OR REPLACE INTO pathways (pathway_id, view, eligible, historical_eligible, stale, hops, verdict, scientific_verdict, data_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (
                        pid, 'validated', elig, h_elig, stale, hops,
                        r.get('verdict'), r.get('scientific_verdict'),
                        json.dumps(r, ensure_ascii=False, separators=(',', ':'))
                    ))
                    for doc_key in r.get('target', {}).get('document_keys', []):
                        matched_id = title_to_id.get(doc_key)
                        if matched_id:
                            cur.execute("INSERT OR IGNORE INTO pathway_documents VALUES (?, ?)", (pid, matched_id))
            else:
                cur.execute("INSERT OR REPLACE INTO global_views VALUES (?, ?)", (kind, raw_text))

    # 4. Documents Views, Claims & Jobs
    docs_dir = snapshot_dir / 'documents'
    if docs_dir.is_dir():
        for doc_folder in docs_dir.iterdir():
            if not doc_folder.is_dir():
                continue
            doc_id = doc_folder.name
            for f in doc_folder.glob('*.json'):
                kind = f.stem
                raw_text = f.read_text(encoding='utf-8')
                if kind == 'claims':
                    claims_data = json.loads(raw_text)
                    meta = {k: v for k, v in claims_data.items() if k != 'claims'}
                    cur.execute("INSERT OR REPLACE INTO document_views VALUES (?, ?, ?)",
                                (doc_id, 'claims-meta', json.dumps(meta, ensure_ascii=False, separators=(',', ':'))))
                    for c in claims_data.get('claims', []):
                        truncate_context_packet(c, max_chars=500)
                        cid = c.get('candidate', {}).get('candidate_id', '')
                        subj = c.get('candidate', {}).get('subject', '')
                        rel = c.get('candidate', {}).get('relation', '')
                        obj = c.get('candidate', {}).get('object', '')
                        val = c.get('validation') or {}
                        verd = val.get('decision', '')
                        cur.execute("INSERT OR REPLACE INTO claims VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                            cid, doc_id, cid, subj, rel, obj, verd,
                            json.dumps(c, ensure_ascii=False, separators=(',', ':'))
                        ))
                elif kind == 'pathway-assessments':
                    p_data = json.loads(raw_text)
                    meta = {k: v for k, v in p_data.items() if k != 'records'}
                    cur.execute("INSERT OR REPLACE INTO document_views VALUES (?, ?, ?)",
                                (doc_id, 'pathway-assessments-meta', json.dumps(meta, ensure_ascii=False, separators=(',', ':'))))
                    for r in p_data.get('records', []):
                        pid = r.get('pathway_id', '')
                        cur.execute("INSERT OR IGNORE INTO pathway_documents VALUES (?, ?)", (pid, doc_id))
                else:
                    cur.execute("INSERT OR REPLACE INTO document_views VALUES (?, ?, ?)", (doc_id, kind, raw_text))

            jobs_dir = doc_folder / 'jobs'
            if jobs_dir.is_dir():
                for jf in jobs_dir.glob('*.json'):
                    jid = jf.stem
                    jdata = jf.read_text(encoding='utf-8')
                    jdict = json.loads(jdata)
                    cur.execute("INSERT OR REPLACE INTO jobs VALUES (?, ?, ?, ?)",
                                (jid, doc_id, jdict.get('status'), jdata))

    conn.commit()
    cur.execute("VACUUM;")
    conn.close()
    return db_file


class PublicationDB:
    """Fast read-only database reader for the results viewer."""

    def __init__(self, db_path: Path | str):
        self.db_path = Path(db_path).resolve()
        self._available: Optional[bool] = None

    def is_available(self) -> bool:
        if self._available is None:
            self._available = self.db_path.is_file() and self.db_path.stat().st_size > 0
        return self._available

    @contextmanager
    def connect(self):
        """Context manager yielding a read-only SQLite connection."""
        if not self.is_available():
            raise FileNotFoundError(f"Database not available at {self.db_path}")
        uri = self.db_path.as_uri() + '?mode=ro'
        conn = sqlite3.connect(uri, uri=True, check_same_thread=False)
        try:
            yield conn
        finally:
            conn.close()

    def get_catalog_raw(self) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute("SELECT value FROM metadata WHERE key = 'catalog'").fetchone()
            return row[0] if row else None

    def get_document(self, doc_id: str) -> Optional[dict]:
        with self.connect() as conn:
            row = conn.execute("SELECT data_json FROM documents WHERE id = ?", (doc_id,)).fetchone()
            return json.loads(row[0]) if row else None

    def get_document_view_raw(self, doc_id: str, kind: str) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute("SELECT data_json FROM document_views WHERE document_id = ? AND kind = ?",
                               (doc_id, kind)).fetchone()
            return row[0] if row else None

    def get_global_view_raw(self, kind: str) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute("SELECT data_json FROM global_views WHERE kind = ?", (kind,)).fetchone()
            return row[0] if row else None

    def get_claims_response_raw(self, doc_id: str, limit: int = 0, offset: int = 0) -> Optional[str]:
        """Constructs claims JSON response directly from stored JSON strings without deserialization."""
        with self.connect() as conn:
            doc_exists = conn.execute("SELECT 1 FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if not doc_exists:
                return None
            if limit > 0:
                rows = conn.execute("SELECT data_json FROM claims WHERE document_id = ? LIMIT ? OFFSET ?",
                                    (doc_id, limit, offset)).fetchall()
            else:
                rows = conn.execute("SELECT data_json FROM claims WHERE document_id = ?", (doc_id,)).fetchall()
            meta_row = conn.execute("SELECT data_json FROM document_views WHERE document_id = ? AND kind = 'claims-meta'",
                                    (doc_id,)).fetchone()
            meta = json.loads(meta_row[0]) if meta_row else {}
            joined_claims = ",".join(r[0] for r in rows)
            meta_parts = [f'"{k}":{json.dumps(v, ensure_ascii=False, separators=(",", ":"))}'
                          for k, v in meta.items() if k != 'claims']
            meta_str = (',' + ','.join(meta_parts)) if meta_parts else ''
            return f'{{"claims":[{joined_claims}]{meta_str}}}'

    def get_document_pathway_assessments_raw(self, doc_id: str) -> Optional[str]:
        """Returns pathway assessments associated with a specific document."""
        with self.connect() as conn:
            doc_exists = conn.execute("SELECT 1 FROM documents WHERE id = ?", (doc_id,)).fetchone()
            if not doc_exists:
                return None
            rows = conn.execute("""
            SELECT p.data_json FROM pathways p
            JOIN pathway_documents pd ON p.pathway_id = pd.pathway_id
            WHERE pd.document_id = ?
            ORDER BY p.pathway_id
            """, (doc_id,)).fetchall()
            meta_row = conn.execute("SELECT data_json FROM document_views WHERE document_id = ? AND kind = 'pathway-assessments-meta'",
                                    (doc_id,)).fetchone()
            meta = json.loads(meta_row[0]) if meta_row else {}
            joined = ",".join(r[0] for r in rows)
            meta_parts = [f'"{k}":{json.dumps(v, ensure_ascii=False, separators=(",", ":"))}'
                          for k, v in meta.items() if k != 'records']
            meta_str = (',' + ','.join(meta_parts)) if meta_parts else ''
            return f'{{"records":[{joined}]{meta_str}}}'

    def get_global_pathway_assessments_raw(self) -> Optional[str]:
        """Returns all global pathway assessments."""
        with self.connect() as conn:
            rows = conn.execute("SELECT data_json FROM pathways WHERE view = 'validated' ORDER BY pathway_id").fetchall()
            meta_row = conn.execute("SELECT data_json FROM global_views WHERE kind = 'pathway-assessments-meta'").fetchone()
            meta = json.loads(meta_row[0]) if meta_row else {}
            joined = ",".join(r[0] for r in rows)
            meta_parts = [f'"{k}":{json.dumps(v, ensure_ascii=False, separators=(",", ":"))}'
                          for k, v in meta.items() if k != 'records']
            meta_str = (',' + ','.join(meta_parts)) if meta_parts else ''
            return f'{{"records":[{joined}]{meta_str}}}'

    def get_pathways_page(self, limit: int = 100, max_hops: int = 6, offset: int = 0,
                          view: str = 'validated') -> Optional[dict]:
        """Fetches paginated pathways using an indexed SQLite query."""
        with self.connect() as conn:
            base_view_row = conn.execute("SELECT data_json FROM global_views WHERE kind = ?",
                                         (f'pathways-{view}',)).fetchone()
            if not base_view_row:
                return None
            data = json.loads(base_view_row[0])

            if view == 'validated' and data.get('policy') in {'whole-pathway-v1', 'neighbor-pair-v1'}:
                # Fetch all matching pathway records
                rows = conn.execute("""
                SELECT data_json FROM pathways
                WHERE view = 'validated'
                  AND (eligible = 1 OR historical_eligible = 1)
                  AND stale = 0
                  AND hops <= ?
                ORDER BY pathway_id
                """, (max_hops,)).fetchall()
                all_records = [json.loads(r[0]) for r in rows]
                page = all_records[offset:offset + limit]
                valid_ids = {e['id'] for r in all_records for e in r.get('target', {}).get('edges', [])}
                edges = [e for e in data['graph']['edges'] if e['id'] in valid_ids]
                node_ids = {e[k] for e in edges for k in ('source', 'target')}
                return {
                    **data,
                    'paths': [r.get('target', {}).get('edges', []) for r in page],
                    'pathway_assessments': page,
                    'graph': {
                        **data['graph'],
                        'edges': edges,
                        'nodes': [n for n in data['graph']['nodes'] if n['id'] in node_ids]
                    },
                    'counts': {
                        'claims': len(edges),
                        'entities': len(node_ids),
                        'papers': len({e['document_key'] for e in edges if 'document_key' in e})
                    },
                    'has_more': offset + limit < len(all_records),
                    'offset': offset,
                    'limit': limit,
                    'max_hops': max_hops
                }
            return None  # Fall back to standard assembled_paths if not validated policy

    def get_jobs(self, doc_id: str) -> List[dict]:
        with self.connect() as conn:
            rows = conn.execute("SELECT data_json FROM jobs WHERE document_id = ?", (doc_id,)).fetchall()
            jobs = [json.loads(r[0]) for r in rows]
            return sorted(
                [{k: j.get(k) for k in ('id', 'pdf_id', 'status', 'recorded_status', 'action', 'started_at', 'finished_at', 'error')}
                 for j in jobs],
                key=lambda j: j.get('started_at') or '',
                reverse=True
            )

    def get_job_raw(self, job_id: str) -> Optional[str]:
        with self.connect() as conn:
            row = conn.execute("SELECT data_json FROM jobs WHERE id = ?", (job_id,)).fetchone()
            return row[0] if row else None
