"""Dense semantic class index (BGE bi-encoder) and Reciprocal Rank Fusion (RRF)."""
from __future__ import annotations

from collections import OrderedDict, defaultdict
from contextlib import closing
from functools import lru_cache
import json
import os
from pathlib import Path
import sqlite3
import threading

import numpy as np

from backend.shared.storage import file_hash

MODEL_NAME = 'BAAI/bge-base-en-v1.5'
QUERY_INSTRUCTION = 'Represent this sentence for searching relevant passages: '
VECTORS_FILE = 'core_bge_vectors.npy'
META_FILE = 'core_bge_meta.json'
EXCLUDED_DENSE_SOURCES = frozenset({'ncbitaxon.owl.gz', 'chebi.owl.gz'})
EXCLUDED_DENSE_MULTIPLIER = 1.45
RRF_K = 35
_SOURCE_KIND_WEIGHTS = {
    'trait or measurement': {'cmo.owl': 0.045, 'oba.owl': 0.045, 'to.owl': 0.045, 'co_330.owl': 0.040, 'pato.owl': 0.040, 'cdno.owl': 0.040},
    'assay variable': {'cmo.owl': 0.045, 'oba.owl': 0.045, 'co_330.owl': 0.045, 'to.owl': 0.040, 'pato.owl': 0.040},
    'condition or exposure': {'peco.owl': 0.060, 'ecto.owl': 0.035, 'envo.owl': 0.025},
    'biological process': {'go.owl': 0.045},
    'anatomy or part': {'po.owl': 0.045, 'to.owl': 0.035},
    'food': {'foodon.owl': 0.045, 'po.owl': 0.035},
    'food composition': {'cdno.owl': 0.045},
    'health outcome': {'mondo.owl': 0.045, 'hp.owl': 0.045, 'go.owl': 0.030},
}


def format_term_passage(info: dict) -> str:
    """Format one ontology class label, exact synonyms, and definition for dense indexing."""
    label = ' '.join(str(info.get('label') or '').split())
    seen = {label.casefold()} if label else set()
    syns = []
    for syn in info.get('exact_synonyms') or ():
        clean = ' '.join(str(syn or '').split())
        if clean and clean.casefold() not in seen:
            seen.add(clean.casefold())
            syns.append(clean)
            if len(syns) >= 6:
                break
    defn = ' '.join(str(info.get('definition') or '').split())
    if len(defn) > 260:
        defn = defn[:257].rsplit(' ', 1)[0] + '...'
    head = f"{label} ({'; '.join(syns)})" if syns else label
    if head and defn:
        return f'{head}: {defn}'
    return head or defn or str(info.get('iri') or '')


def rrf_fuse(channels, *, k: int = RRF_K, source_multipliers: dict[str, float] | None = None) -> dict[str, dict]:
    """Combine multiple ranked hit lists using Reciprocal Rank Fusion (weight / (k + rank))."""
    fused: dict[str, dict] = {}
    for channel_name, weight, hits in channels:
        if not weight or not hits:
            continue
        seen_in_channel = set()
        rank = 0
        for hit in hits:
            iri = hit.get('iri')
            if not iri or iri in seen_in_channel:
                continue
            seen_in_channel.add(iri)
            rank += 1
            entry = fused.get(iri)
            if entry is None:
                entry = {
                    'iri': iri,
                    'class_source': hit.get('class_source'),
                    'rrf_score': 0.0,
                    'ranks': {},
                }
                fused[iri] = entry
            elif not entry.get('class_source') and hit.get('class_source'):
                entry['class_source'] = hit.get('class_source')
            entry['rrf_score'] += float(weight) / (k + rank)
            entry['ranks'][channel_name] = rank
    if source_multipliers:
        for entry in fused.values():
            mult = source_multipliers.get(entry.get('class_source') or '', 1.0)
            if mult != 1.0:
                entry['rrf_score'] *= mult
    return fused


def _resolve_device(device: str | None = None) -> str:
    if device:
        return device
    if os.environ.get('EMBEDDING_DEVICE'):
        return os.environ['EMBEDDING_DEVICE']
    try:
        import torch
        if torch.cuda.is_available():
            return 'cuda'
    except Exception:
        pass
    return 'cpu'


def _load_bge_encoder(device: str):
    from sentence_transformers import SentenceTransformer

    try:
        return SentenceTransformer(MODEL_NAME, device=device, local_files_only=True)
    except OSError:
        return SentenceTransformer(MODEL_NAME, device=device)


def build_semantic_index(db_path: Path, out_dir: Path, *, batch_size: int = 256, device: str | None = None) -> dict:
    """Encode non-taxon, non-ChEBI classes from scoped SQLite into normalized float16 vectors."""
    db_path = Path(db_path).resolve()
    out_dir = Path(out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    dev = _resolve_device(device)

    placeholders = ','.join('?' for _ in sorted(EXCLUDED_DENSE_SOURCES))
    with closing(sqlite3.connect(db_path.as_uri() + '?mode=ro', uri=True)) as db:
        rows = db.execute(
            f"SELECT t.iri, f.source, t.info FROM terms_fts f JOIN terms t USING (iri) "
            f"WHERE f.kind='class' AND NOT t.obsolete AND f.source NOT IN ({placeholders}) "
            f"ORDER BY t.iri",
            tuple(sorted(EXCLUDED_DENSE_SOURCES)),
        ).fetchall()

    iris, sources, passages = [], [], []
    for iri, src, raw_info in rows:
        info = json.loads(raw_info)
        iris.append(iri)
        sources.append(src)
        passages.append(format_term_passage(info))

    encoder = _load_bge_encoder(dev)
    matrix = encoder.encode(
        passages,
        batch_size=batch_size,
        normalize_embeddings=True,
        convert_to_numpy=True,
        show_progress_bar=False,
    )
    vectors = np.asarray(matrix, dtype=np.float16)

    vectors_path = out_dir / VECTORS_FILE
    tmp_vectors = out_dir / ('.' + VECTORS_FILE)
    with tmp_vectors.open('wb') as stream:
        np.save(stream, vectors)
    tmp_vectors.replace(vectors_path)

    meta = {
        'model': MODEL_NAME,
        'query_instruction': QUERY_INSTRUCTION,
        'count': len(iris),
        'dim': int(vectors.shape[1]) if vectors.ndim == 2 else 0,
        'dtype': 'float16',
        'excluded_sources': sorted(EXCLUDED_DENSE_SOURCES),
        'db_sha256': file_hash(db_path),
        'vectors_sha256': file_hash(vectors_path),
        'iris': iris,
        'sources': sources,
    }
    meta_path = out_dir / META_FILE
    tmp_meta = out_dir / ('.' + META_FILE + '.tmp')
    tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, separators=(',', ':')), encoding='utf-8')
    tmp_meta.replace(meta_path)
    return {
        'count': meta['count'],
        'dim': meta['dim'],
        'device': dev,
        'vectors_path': str(vectors_path),
        'meta_path': str(meta_path),
        'vectors_sha256': meta['vectors_sha256'],
        'db_sha256': meta['db_sha256'],
    }


class SemanticClassIndex:
    """Thread-safe dense cosine search over core OBO ontology classes."""

    def __init__(self, vectors_path: Path, meta_path: Path, *, device: str | None = None):
        meta = json.loads(Path(meta_path).read_text(encoding='utf-8'))
        vectors = np.load(Path(vectors_path))
        if vectors.ndim != 2 or vectors.shape[0] != len(meta.get('iris', ())):
            raise ValueError('Semantic class index shape mismatch against metadata.')
        self.meta = meta
        self.iris: list[str] = meta['iris']
        self.sources: list[str] = meta['sources']
        self.model_name: str = meta.get('model') or MODEL_NAME
        self.query_instruction: str = meta.get('query_instruction') or QUERY_INSTRUCTION
        self.device = _resolve_device(device)
        self._lock = threading.RLock()
        self._encoder = None
        self._query_cache: OrderedDict[str, np.ndarray] = OrderedDict()
        self._gpu_matrix = None
        if self.device.startswith('cuda'):
            try:
                import torch
                if torch.cuda.is_available():
                    self._gpu_matrix = torch.from_numpy(np.ascontiguousarray(vectors)).to(
                        device=self.device, dtype=torch.float16
                    )
            except Exception:
                self._gpu_matrix = None
                self.device = 'cpu'
        if self._gpu_matrix is None:
            self._cpu_matrix = np.asarray(vectors, dtype=np.float32)
        else:
            self._cpu_matrix = None

    def _encode_query(self, text: str) -> np.ndarray:
        with self._lock:
            cached = self._query_cache.get(text)
            if cached is not None:
                self._query_cache.move_to_end(text)
                return cached
            if self._encoder is None:
                self._encoder = _load_bge_encoder(self.device)
            vec = self._encoder.encode(
                self.query_instruction + text,
                normalize_embeddings=True,
                convert_to_numpy=True,
                show_progress_bar=False,
            )
            arr = np.asarray(vec, dtype=np.float32).reshape(-1)
            self._query_cache[text] = arr
            if len(self._query_cache) > 2048:
                self._query_cache.popitem(last=False)
            return arr

    def search(self, phrase: str, *, definitions=(), kind: str = '', limit: int = 40, min_score: float = 0.52) -> list[dict]:
        from backend.evaluation.experimental_retrieval import _DOSE_CODE_RE, _expand_query_phrase

        clean = ' '.join(str(phrase or '').split())
        if not clean or _DOSE_CODE_RE.match(clean) or not any(ch.isalpha() for ch in clean):
            return []
        variants = _expand_query_phrase(clean, definitions or ())
        expanded = variants[-1]
        if len(variants) == 1 and len(clean) <= 3 and clean.isupper():
            return []

        q_vec = self._encode_query(expanded)
        preferred_weights = _SOURCE_KIND_WEIGHTS.get(kind, {})
        pool_k = min(max(limit * 3, 120), len(self.iris))
        if self._gpu_matrix is not None:
            import torch
            with torch.no_grad():
                q_t = torch.from_numpy(q_vec).to(device=self.device, dtype=torch.float16)
                scores_t = torch.mv(self._gpu_matrix, q_t)
                top_vals, top_idx = torch.topk(scores_t, k=pool_k)
                indices = top_idx.cpu().tolist()
                scores = top_vals.float().cpu().tolist()
        else:
            all_scores = self._cpu_matrix @ q_vec
            if pool_k >= len(all_scores):
                order = np.argsort(-all_scores)
            else:
                top_part = np.argpartition(-all_scores, pool_k - 1)[:pool_k]
                order = top_part[np.argsort(-all_scores[top_part])]
            indices = order.tolist()
            scores = [float(all_scores[i]) for i in indices]

        candidates = []
        for idx, raw_score in zip(indices, scores):
            if raw_score < min_score:
                continue
            src = self.sources[idx]
            adj_score = raw_score + preferred_weights.get(src, 0.0)
            candidates.append((adj_score, raw_score, self.iris[idx], src))
        candidates.sort(key=lambda item: (-item[0], item[2]))
        return [
            {
                'iri': iri,
                'class_source': src,
                'score': round(float(raw_score), 4),
                'via': 'dense_bge',
            }
            for _, raw_score, iri, src in candidates[:limit]
        ]


@lru_cache(maxsize=2)
def open_semantic_index(root: str | Path) -> SemanticClassIndex | None:
    """Open persisted BGE class index in ``root`` if built; otherwise return None."""
    base = Path(root).resolve()
    vectors_path = base / VECTORS_FILE
    meta_path = base / META_FILE
    if not (vectors_path.is_file() and meta_path.is_file()):
        return None
    return SemanticClassIndex(vectors_path, meta_path)
