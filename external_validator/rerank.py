"""HyDE, dense retrieval, and true cross-encoder reranking."""

from __future__ import annotations

from typing import Any

from .config import Settings
from .llm import OllamaClient


_HYPOTHESIS_SCHEMA = {
    "type": "object",
    "properties": {"hypothesis": {"type": "string"}},
    "required": ["hypothesis"],
    "additionalProperties": False,
}


class SemanticReranker:
    """Two-stage BGE bi-encoder retrieval plus cross-encoder reranking."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._embedding_runtime: Any | None = None
        self._cross_runtime: Any | None = None

    def _load_embedding_model(self) -> Any:
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
        dense_backend, _ = self._load_embedding_model()
        cross_backend, _ = self._load_cross_encoder()
        return f"dense={dense_backend}; cross_encoder={cross_backend}"

    def rank_texts(self, hypothesis: str, texts: list[str]) -> list[float]:
        """Return BGE cosine similarity for first-stage dense retrieval."""
        if not texts:
            return []
        backend, model = self._load_embedding_model()
        # BGE v1.5 was trained with a retrieval instruction on the query side;
        # passages remain unprefixed. Keeping this in Settings also makes the
        # behavior explicit if a future embedding model needs no instruction.
        query = f"{self.settings.embedding_query_instruction}{hypothesis}"
        values = [query, *texts]
        if backend == "sentence-transformers":
            embeddings = model.encode(values, normalize_embeddings=True, show_progress_bar=False)
        else:
            torch, tokenizer, encoder, device = model
            with torch.inference_mode():
                batch = tokenizer(values, padding=True, truncation=True, max_length=512, return_tensors="pt")
                batch = {name: value.to(device) for name, value in batch.items()}
                embeddings = torch.nn.functional.normalize(
                    encoder(**batch).last_hidden_state[:, 0], p=2, dim=1).cpu().numpy()
        query_vector = embeddings[0]
        return [float(query_vector @ vector) for vector in embeddings[1:]]

    def cross_scores(self, query: str, texts: list[str]) -> list[float]:
        """Score query/passage pairs jointly with a genuine cross-encoder."""
        if not texts:
            return []
        backend, model = self._load_cross_encoder()
        pairs = [(query, text) for text in texts]
        if backend == "sentence-transformers":
            raw = model.predict(pairs, show_progress_bar=False)
            return [float(value[0] if getattr(value, "ndim", 0) else value) for value in raw]
        torch, tokenizer, encoder, device = model
        scores: list[float] = []
        for start in range(0, len(pairs), 8):
            batch_pairs = pairs[start:start + 8]
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

    def select(self, hypothesis: str, chunks: list[Any], article_key: str, top_chunks: int) -> list[dict[str, Any]]:
        if not chunks:
            return []
        records = [item if isinstance(item, dict) else {"text": str(item)} for item in chunks]
        texts = [str(item.get("text") or "") for item in records]
        dense_scores = self.rank_texts(hypothesis, texts)
        dense_candidates = sorted(range(len(records)), key=lambda index: dense_scores[index], reverse=True)[
            :max(top_chunks * 6, self.settings.cross_encoder_candidates)
        ]
        cross_scores = self.cross_scores(hypothesis, [texts[index] for index in dense_candidates])
        reranked = sorted(zip(cross_scores, dense_candidates), reverse=True)[:top_chunks]
        selected: list[dict[str, Any]] = []
        for cross_score, index in reranked:
            record = records[index]
            selected.append({
                "chunk_id": f"{article_key}:chunk-{index}",
                "dense_score": round(dense_scores[index], 6),
                "cross_encoder_score": round(float(cross_score), 6),
                "text": texts[index],
                **({"page_number": record["page_number"]} if record.get("page_number") else {}),
            })
        return selected


def generate_hypothesis(client: OllamaClient, candidate: dict[str, Any]) -> str:
    system = "You write one neutral, hypothetical scientific conclusion for semantic retrieval. Do not assert that the claim is true. Output JSON only."
    user = ("Create one single-sentence academic passage that would directly discuss this claim. "
            f"Claim: <{candidate['subject']}, {candidate['relation']}, {candidate['object']}>. "
            "Return {\"hypothesis\": \"...\"}.")
    response = client.chat_json(system, user, max_tokens=96, json_schema=_HYPOTHESIS_SCHEMA)
    hypothesis = str(response.get("hypothesis", "")).strip()
    if not hypothesis:
        raise RuntimeError("Qwen did not return a HyDE hypothesis")
    return hypothesis
