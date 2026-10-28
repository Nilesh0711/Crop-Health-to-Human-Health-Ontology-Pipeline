"""Rank evidence passages using BGE and MedCPT without establishing scientific support."""

from __future__ import annotations

from backend.shared.cancellation import check_cancelled
from typing import Any
from collections import OrderedDict
from hashlib import sha256
import re
from pathlib import Path
from backend.config.settings import Settings
from backend.providers.runtime_pool import runtime, inference


def embedding_revision(backend, model, name):
    """Persist vectors only for identifiable weights, never a mutable name alone."""
    encoder = model[2] if backend == 'transformers-fallback' else model
    if backend == 'sentence-transformers':
        try:
            encoder = getattr(model[0], 'auto_model', model)
        except (TypeError, IndexError, KeyError):
            encoder = model
    revision = getattr(getattr(encoder, 'config', None), '_commit_hash', None)
    if isinstance(revision, str) and revision:
        return revision
    directory = Path(name)
    if directory.is_dir():
        files = sorted(p for p in directory.rglob('*') if p.is_file() and p.suffix in
                       {'.json', '.safetensors', '.bin', '.model', '.txt'})
        return sha256(repr([(str(p.relative_to(directory)), p.stat().st_size, p.stat().st_mtime_ns)
                            for p in files]).encode()).hexdigest() if files else None
    return None


class SemanticReranker:
    """Two-stage BGE bi-encoder retrieval plus cross-encoder reranking."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._embedding_runtime: Any | None = None
        self._cross_runtime: Any | None = None
        self._embedding_cache: OrderedDict[str, Any] = OrderedDict()
        self.embedding_cache_hits = 0
        self.evidence_index = None

    def _load_embedding_model(self) -> Any:
        if self._embedding_runtime is None:
            key = ("embedding", self.settings.embedding_model, self.settings.embedding_device, type(self)._create_embedding_model)
            self._embedding_runtime = runtime(key, self._create_embedding_model)
        return self._embedding_runtime

    def _create_embedding_model(self) -> Any:
        if self._embedding_runtime is None:
            try:
                from sentence_transformers import SentenceTransformer
                model = SentenceTransformer(self.settings.embedding_model, device=self.settings.embedding_device)
                self._embedding_runtime = ("sentence-transformers", model)
            except (ImportError, OSError):
                try:
                    import torch
                    from transformers import AutoModel, AutoTokenizer
                except ImportError as exc:
                    raise RuntimeError("Semantic retrieval requires sentence-transformers or torch + transformers") from exc
                device = self.settings.embedding_device
                if device.startswith("cuda") and not torch.cuda.is_available():
                    device = "cpu"
                tokenizer = AutoTokenizer.from_pretrained(self.settings.embedding_model)
                model = AutoModel.from_pretrained(self.settings.embedding_model).to(device)
                model.eval()
                self._embedding_runtime = ("transformers-fallback", (torch, tokenizer, model, device))
        return self._embedding_runtime

    def _load_cross_encoder(self) -> Any:
        if self._cross_runtime is None:
            key = ("cross", self.settings.cross_encoder_model, self.settings.cross_encoder_device, type(self)._create_cross_encoder)
            self._cross_runtime = runtime(key, self._create_cross_encoder)
        return self._cross_runtime

    def _create_cross_encoder(self) -> Any:
        if self._cross_runtime is None:
            try:
                from sentence_transformers import CrossEncoder
                model = CrossEncoder(self.settings.cross_encoder_model, device=self.settings.cross_encoder_device)
                self._cross_runtime = ("sentence-transformers", model)
            except (ImportError, OSError):
                try:
                    import torch
                    from transformers import AutoModelForSequenceClassification, AutoTokenizer
                except ImportError as exc:
                    raise RuntimeError("Cross-encoder reranking requires sentence-transformers or torch + transformers") from exc
                device = self.settings.cross_encoder_device
                if device.startswith("cuda") and not torch.cuda.is_available():
                    device = "cpu"
                tokenizer = AutoTokenizer.from_pretrained(self.settings.cross_encoder_model)
                model = AutoModelForSequenceClassification.from_pretrained(self.settings.cross_encoder_model).to(device)
                model.eval()
                self._cross_runtime = ("transformers-fallback", (torch, tokenizer, model, device))
        return self._cross_runtime

    def warmup(self) -> str:
        check_cancelled()
        dense_backend, _ = self._load_embedding_model()
        cross_backend, _ = self._load_cross_encoder()
        def device(runtime):
            backend, model = runtime
            return str(getattr(model, 'device', 'unknown')) if backend == 'sentence-transformers' else str(model[-1])
        return (f"dense={dense_backend} ({device(self._embedding_runtime)}), batch={self.settings.embedding_batch_size}; "
                f"cross_encoder={cross_backend} ({device(self._cross_runtime)}), batch={self.settings.cross_encoder_batch_size}")

    def rank_texts(self, hypothesis: str, texts: list[str]) -> list[float]:
        """Return BGE cosine similarity for first-stage dense retrieval."""
        if not texts:
            return []
        check_cancelled()
        backend, model = self._load_embedding_model()
        # BGE v1.5 was trained with a retrieval instruction on the query side;
        # passages remain unprefixed. Keeping this in Settings also makes the
        # behavior explicit if a future embedding model needs no instruction.
        query = f"{self.settings.embedding_query_instruction}{hypothesis}"
        values = [query, *texts]
        revision = embedding_revision(backend, model, self.settings.embedding_model)
        identity = revision or f'process-runtime:{id(model)}'
        keys = [sha256(f"{backend}|{self.settings.embedding_model}|{identity}|{value}".encode("utf-8")).hexdigest() for value in values]
        vectors = {key: self._embedding_cache[key] for key in keys if key in self._embedding_cache}
        self.embedding_cache_hits += sum(key in vectors for key in keys)
        if self.evidence_index is not None and revision:
            import numpy as np
            stored_vectors = self.evidence_index.get_many(['embedding-v1:' + key for key in dict.fromkeys(keys)
                                                          if key not in vectors], expiring=False)
            for key in dict.fromkeys(keys):
                if key not in vectors:
                    stored = stored_vectors.get('embedding-v1:' + key)
                    if stored is not None:
                        vectors[key] = np.asarray(stored, dtype=np.float32)
                        self._embedding_cache[key] = vectors[key]
                        self.embedding_cache_hits += 1
        missing = list(dict.fromkeys(key for key in keys if key not in vectors))
        text_by_key = dict(zip(keys, values))
        for start in range(0, len(missing), self.settings.embedding_batch_size):
            batch_keys = missing[start:start + self.settings.embedding_batch_size]
            with inference(self.settings.embedding_device):
                embeddings = self._encode(backend, model, [text_by_key[key] for key in batch_keys])
            for key, vector in zip(batch_keys, embeddings):
                vectors[key] = vector
                self._embedding_cache[key] = vector
            if self.evidence_index is not None and revision:
                self.evidence_index.put_many({'embedding-v1:' + key: vectors[key].tolist() for key in batch_keys})
        for key in keys:
            if key in self._embedding_cache:
                self._embedding_cache.move_to_end(key)
        while len(self._embedding_cache) > 4096:
            self._embedding_cache.popitem(last=False)
        query_vector = vectors[keys[0]]
        return [float(query_vector @ vectors[key]) for key in keys[1:]]

    def _encode(self, backend: str, model: Any, values: list[str]) -> Any:
        if backend == "sentence-transformers":
            return model.encode(values, normalize_embeddings=True, show_progress_bar=False, batch_size=self.settings.embedding_batch_size)
        else:
            torch, tokenizer, encoder, device = model
            with torch.inference_mode():
                batch = tokenizer(values, padding=True, truncation=True, max_length=512, return_tensors="pt")
                batch = {name: value.to(device) for name, value in batch.items()}
                return torch.nn.functional.normalize(
                    encoder(**batch).last_hidden_state[:, 0], p=2, dim=1).cpu().numpy()

    def cross_scores(self, query: str, texts: list[str]) -> list[float]:
        with inference(self.settings.cross_encoder_device):
            return self._cross_scores(query, texts)

    def _cross_scores(self, query: str, texts: list[str]) -> list[float]:
        """Score query/passage pairs jointly with a genuine cross-encoder."""
        if not texts:
            return []
        check_cancelled()
        backend, model = self._load_cross_encoder()
        pairs = [(query, text) for text in texts]
        if backend == "sentence-transformers":
            raw = model.predict(pairs, show_progress_bar=False, batch_size=self.settings.cross_encoder_batch_size)
            return [float(value[0] if getattr(value, "ndim", 0) else value) for value in raw]
        torch, tokenizer, encoder, device = model
        scores: list[float] = []
        for start in range(0, len(pairs), self.settings.cross_encoder_batch_size):
            batch_pairs = pairs[start:start + self.settings.cross_encoder_batch_size]
            encoded = tokenizer([item[0] for item in batch_pairs], [item[1] for item in batch_pairs],
                                padding=True, truncation=True, max_length=512, return_tensors="pt")
            encoded = {name: value.to(device) for name, value in encoded.items()}
            with torch.inference_mode():
                logits = encoder(**encoded).logits
            if logits.shape[-1] == 1:
                scores.extend(float(value) for value in logits[:, 0].cpu().tolist())
            else:
                probabilities = torch.softmax(logits, dim=-1)
                scores.extend(float(value) for value in probabilities[:, -1].cpu().tolist())
        return scores

    def rerank(self, query: str, texts: list[str]) -> list[tuple[float, int]]:
        scores = self.cross_scores(query, texts)
        return sorted(((score, index) for index, score in enumerate(scores)), reverse=True)

    def select(self, hypothesis: str, chunks: list[Any], article_key: str, top_chunks: int,
               neutral_query: str | None = None) -> list[dict[str, Any]]:
        if not chunks:
            return []
        records = [item if isinstance(item, dict) else {"text": str(item)} for item in chunks]
        texts = [str(item.get("text") or "") for item in records]
        dense_scores = self.rank_texts(hypothesis, texts)
        neutral_scores = self.rank_texts(neutral_query, texts) if neutral_query else dense_scores
        query_terms = set(re.findall(r"[a-z0-9]+", hypothesis.casefold()))
        lexical = [len(query_terms & set(re.findall(r"[a-z0-9]+", text.casefold()))) for text in texts]
        kinds = [passage_kind(text, str(record.get("section_heading") or "")) for record, text in zip(records, texts)]
        dense_order = sorted(range(len(records)), key=lambda index: dense_scores[index], reverse=True)
        lexical_order = sorted(range(len(records)), key=lambda index: lexical[index], reverse=True)
        dense_rank = {index: rank for rank, index in enumerate(dense_order, 1)}
        neutral_order = sorted(range(len(records)), key=lambda index: neutral_scores[index], reverse=True)
        neutral_rank = {index: rank for rank, index in enumerate(neutral_order, 1)}
        lexical_rank = {index: rank for rank, index in enumerate(lexical_order, 1)}
        bonuses = {"result": 0.005, "methods": -0.005, "other": 0.0}
        initial = {index: 1 / (60 + dense_rank[index]) + 1 / (60 + neutral_rank[index])
                   + 1 / (60 + lexical_rank[index]) + bonuses[kinds[index]]
                   for index in range(len(records))}
        dense_candidates = sorted(range(len(records)), key=lambda index: initial[index], reverse=True)[
            :max(top_chunks * 6, self.settings.cross_encoder_candidates)
        ]
        cross_scores = self.cross_scores(hypothesis, [texts[index] for index in dense_candidates])
        neutral_cross = self.cross_scores(neutral_query, [texts[index] for index in dense_candidates]) if neutral_query else cross_scores
        neutral_cross_by_id = dict(zip(dense_candidates, neutral_cross))
        neutral_cross_order = sorted(dense_candidates, key=lambda i: neutral_cross_by_id[i], reverse=True)
        neutral_cross_rank = {index: rank for rank, index in enumerate(neutral_cross_order, 1)}
        cross_order = sorted(zip(cross_scores, dense_candidates), reverse=True)
        cross_rank = {index: rank for rank, (_score, index) in enumerate(cross_order, 1)}
        initial_rank = {index: rank for rank, index in enumerate(dense_candidates, 1)}
        reranked = sorted(cross_order, key=lambda item: 1 / (60 + cross_rank[item[1]])
                          + 1 / (60 + neutral_cross_rank[item[1]])
                          + 1 / (60 + initial_rank[item[1]]) + bonuses[kinds[item[1]]], reverse=True)[:top_chunks]
        selected: list[dict[str, Any]] = []
        for cross_score, index in reranked:
            record = records[index]
            selected.append({
                "chunk_id": f"{article_key}:chunk-{index}",
                "dense_score": round(dense_scores[index], 6),
                "cross_encoder_score": round(float(cross_score), 6),
                'neutral_dense_score': round(float(neutral_scores[index]), 6),
                'neutral_cross_encoder_score': round(float(neutral_cross_by_id[index]), 6),
                "passage_kind": kinds[index],
                "text": texts[index],
                **{key: record[key] for key in ('source_kind', 'table_id', 'row_number', 'table_headers',
                    'table_cells', 'table_footnotes', 'figure_id', 'layout_verified') if key in record},
                'paragraph_references': record.get('paragraph_references', []),
                **({"page_number": record["page_number"]} if record.get("page_number") else {}),
                **({"section_heading": record["section_heading"]} if record.get("section_heading") else {}),
            })
        return selected


def passage_kind(text: str, heading: str = "") -> str:
    """A ranking hint about reporting function, never a support verdict."""
    if re.search(r"\bresults?|findings?|discussion\b", heading, re.IGNORECASE) or re.search(
            r"\b(?:results (?:show|indicat)|we (?:found|observed)|significantly (?:increas|decreas)|resulted in|was (?:higher|lower))", text, re.IGNORECASE):
        return "result"
    if re.search(r"\b(?:methods?|materials?|protocol|experimental design)\b", heading, re.IGNORECASE) or re.search(
            r"\b(?:the aim|we (?:investigated|tested|measured)|was (?:measured|determined)|were (?:collected|tested))\b", text, re.IGNORECASE):
        return "methods"
    return "other"
