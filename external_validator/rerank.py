"""HyDE generation plus semantic BGE embedding reranking."""

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
    def __init__(self, settings: Settings):
        self.settings = settings
        self._model: Any | None = None

    def _load_model(self) -> Any:
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
                self._model = ("sentence-transformers", SentenceTransformer(self.settings.embedding_model))
            except (ImportError, OSError):
                # This workstation's application-control policy can block SciPy, a
                # transitive optional dependency of sentence-transformers.  Keep the
                # required BGE model and compute its standard CLS-normalized vectors
                # through PyTorch/Transformers instead of silently returning to
                # lexical matching.
                try:
                    import torch
                    from transformers import AutoModel, AutoTokenizer
                except ImportError as exc:
                    raise RuntimeError("Semantic reranking requires sentence-transformers or torch + transformers. Run: pip install -r requirements.txt") from exc
                tokenizer = AutoTokenizer.from_pretrained(self.settings.embedding_model)
                model = AutoModel.from_pretrained(self.settings.embedding_model)
                model.eval()
                self._model = ("transformers-fallback", (torch, tokenizer, model))
        return self._model

    def warmup(self) -> str:
        """Load the embedding model once before per-claim external validation."""
        backend, _ = self._load_model()
        return backend

    def rank_texts(self, hypothesis: str, texts: list[str]) -> list[float]:
        """Return one cosine-similarity score per text without persisting embeddings."""
        if not texts:
            return []
        backend, model = self._load_model()
        values = [hypothesis, *texts]
        if backend == "sentence-transformers":
            embeddings = model.encode(values, normalize_embeddings=True, show_progress_bar=False)
        else:
            torch, tokenizer, encoder = model
            with torch.inference_mode():
                batch = tokenizer(values, padding=True, truncation=True, max_length=512, return_tensors="pt")
                embeddings = torch.nn.functional.normalize(
                    encoder(**batch).last_hidden_state[:, 0], p=2, dim=1).cpu().numpy()
        query_vector = embeddings[0]
        return [float(query_vector @ vector) for vector in embeddings[1:]]

    def select(self, hypothesis: str, chunks: list[str], article_key: str, top_chunks: int) -> list[dict[str, Any]]:
        if not chunks:
            return []
        scores = self.rank_texts(hypothesis, chunks)
        ranked = sorted(((score, index, chunk) for index, (score, chunk) in enumerate(zip(scores, chunks))), reverse=True)
        return [{"chunk_id": f"{article_key}:chunk-{index}", "semantic_score": round(score, 6), "text": chunk}
                for score, index, chunk in ranked[:top_chunks]]


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
