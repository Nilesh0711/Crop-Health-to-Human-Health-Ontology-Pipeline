"""Versioned, bounded SQLite evidence/embedding cache shared across document runs."""
import hashlib
import json
import sqlite3
import time
import re
import zlib
from pathlib import Path
from contextlib import contextmanager


POLICY = 'exact-source-index-v1'


class EvidenceIndexError(RuntimeError):
    """Corrupt cached proof is a visible technical failure, not missing evidence."""


class EvidenceIndex:
    def __init__(self, directory, *, max_bytes=512 * 1024 * 1024, ttl_seconds=7 * 86400):
        self.path = Path(directory) / 'evidence.sqlite3'
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.max_bytes, self.ttl_seconds = max_bytes, ttl_seconds
        self.hits = 0
        with self._connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS entries (key TEXT PRIMARY KEY, content BLOB NOT NULL, '
                       'digest TEXT NOT NULL, saved REAL NOT NULL, used REAL NOT NULL, size INTEGER NOT NULL)')
            db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS papers USING fts5(identity UNINDEXED, title, preview)')

    @contextmanager
    def _connect(self):
        db = sqlite3.connect(self.path, timeout=20)
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, *, expiring=True):
        return self.get_many([key], expiring=expiring).get(key)

    def get_many(self, keys, *, expiring=True):
        results = {}
        with self._connect() as db:
            for start in range(0, len(keys), 400):
                batch = [POLICY + ':' + k for k in keys[start:start + 400]]
                rows = db.execute('SELECT key,content,digest,saved FROM entries WHERE key IN (' +
                                  ','.join('?' for _ in batch) + ')', batch).fetchall()
                for key, blob, digest, saved in rows:
                    if expiring and time.time() - saved > self.ttl_seconds:
                        continue
                    raw = zlib.decompress(blob)
                    if hashlib.sha256(raw).hexdigest() != digest:
                        raise EvidenceIndexError('Stored evidence content hash does not match; refresh the evidence index')
                    results[key.removeprefix(POLICY + ':')] = json.loads(raw)
                    db.execute('UPDATE entries SET used=? WHERE key=?', (time.time(), key))
        self.hits += len(results)
        return results

    def put(self, key, value):
        self.put_many({key: value})

    def put_many(self, entries):
        now = time.time()
        with self._connect() as db:
            for key, value in entries.items():
                raw = json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode()
                blob = zlib.compress(raw)
                if len(blob) <= self.max_bytes:
                    db.execute('INSERT OR REPLACE INTO entries VALUES (?,?,?,?,?,?)',
                               (POLICY + ':' + key, blob, hashlib.sha256(raw).hexdigest(), now, now, len(blob)))
            size = db.execute('SELECT COALESCE(SUM(size),0) FROM entries').fetchone()[0]
            if size > self.max_bytes:
                for name, amount in db.execute('SELECT key,size FROM entries ORDER BY used').fetchall():
                    db.execute('DELETE FROM entries WHERE key=?', (name,))
                    size -= amount
                    if size <= self.max_bytes:
                        break

    def material(self, key, fetch):
        result = self.get(key)
        if result is not None:
            return result
        result = fetch()
        # Failed/unavailable source responses must not become persistent evidence.
        if result and result.get('paragraphs'):
            self.put(key, result)
        return result

    def stats(self):
        with self._connect() as db:
            count, size = db.execute('SELECT COUNT(*),COALESCE(SUM(size),0) FROM entries').fetchone()
        return {'entries': count, 'compressed_bytes': size, 'hits_this_instance': self.hits,
                'ttl_seconds': self.ttl_seconds, 'max_bytes': self.max_bytes, 'policy': POLICY}

    def catalogue(self, identity, record, paragraphs=None):
        """Index bibliographic metadata, never claim-specific citation roles."""
        fields = ('source', 'id', 'doi', 'pmcid', 'pmid', 'title', 'authorString', 'pubYear', 'journalTitle',
                  'abstractText', 'isOpenAccess', '_open_access_pdf_url', '_landing_page_url', '_open_access_license')
        metadata = {k: record.get(k) for k in fields if record.get(k) is not None}
        self.put('paper-meta:' + identity, metadata)
        preview = metadata.get('abstractText', '')[:2000]
        if paragraphs:
            preview += '\n' + '\n'.join(str(p.get('text', '') if isinstance(p, dict) else p)
                                        for p in paragraphs)[:16000]
        with self._connect() as db:
            db.execute('DELETE FROM papers WHERE identity=?', (identity,))
            db.execute('INSERT INTO papers VALUES (?,?,?)', (identity, metadata.get('title', ''), preview))
            # The FTS catalogue is a bounded retrieval aid. Its original full
            # metadata remains content-hashed in entries and expires normally.
            db.execute('DELETE FROM papers WHERE rowid NOT IN (SELECT rowid FROM papers ORDER BY rowid DESC LIMIT 4096)')

    def search_papers(self, text, limit=20):
        terms = list(dict.fromkeys(w.lower() for w in re.findall(r'\w+', text) if len(w) > 2))[:24]
        if not terms:
            return []
        query = ' OR '.join('"' + word + '"' for word in terms)
        with self._connect() as db:
            rows = db.execute('SELECT identity FROM papers WHERE papers MATCH ? ORDER BY rank LIMIT ?', (query, limit)).fetchall()
        stored = self.get_many(['paper-meta:' + r[0] for r in rows])
        return [{**stored['paper-meta:' + r[0]], '_retrieval_route': 'shared_evidence_index'} for r in rows
                if 'paper-meta:' + r[0] in stored]
