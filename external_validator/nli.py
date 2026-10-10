"""Optional independent NLI signal for evidence-entailment decisions."""

from __future__ import annotations

from typing import Any

from .config import Settings


class NLIValidator:
    """Lazy DeBERTa NLI classifier kept separate from the extraction LLM.

    NLI is an auxiliary safeguard, never sufficient evidence by itself.  Exact
    anchors and the evidence-bound Qwen decision remain mandatory.
    """

    def __init__(self, settings: Settings):
        self.settings = settings
        self._runtime: tuple[Any, Any, Any, str] | None = None

    def _load(self) -> tuple[Any, Any, Any, str]:
        if self._runtime is None:
            try:
                import torch
                from transformers import AutoModelForSequenceClassification, AutoTokenizer
            except ImportError as exc:
                raise RuntimeError("NLI validation requires torch and transformers") from exc
            device = self.settings.nli_device
            if device.startswith("cuda") and not torch.cuda.is_available():
                device = "cpu"
            tokenizer = AutoTokenizer.from_pretrained(self.settings.nli_model)
            model = AutoModelForSequenceClassification.from_pretrained(self.settings.nli_model)
            model.to(device)
            model.eval()
            self._runtime = (torch, tokenizer, model, device)
        return self._runtime

    def warmup(self) -> str:
        self._load()
        return self.settings.nli_model

    def score(self, premise: str, hypothesis: str) -> dict[str, float]:
        torch, tokenizer, model, device = self._load()
        encoded = tokenizer(premise, hypothesis, truncation=True, max_length=512, return_tensors="pt")
        encoded = {name: value.to(device) for name, value in encoded.items()}
        with torch.inference_mode():
            probabilities = torch.softmax(model(**encoded).logits[0], dim=-1).cpu().tolist()
        labels = {int(index): str(label).casefold() for index, label in model.config.id2label.items()}
        scores = {"contradiction": 0.0, "entailment": 0.0, "neutral": 0.0}
        for index, probability in enumerate(probabilities):
            label = labels.get(index, "")
            if label in scores:
                scores[label] = round(float(probability), 6)
        return scores


def claim_hypothesis(candidate: dict[str, Any]) -> str:
    relation = str(candidate.get("relation") or "").replace("_", " ")
    base = f"{candidate.get('subject', '')} {relation} {candidate.get('object', '')}."
    qualifiers = candidate.get("qualifiers") or {}
    context = "; ".join(f"{name.replace('_', ' ')}: {value}" for name, value in qualifiers.items() if value)
    return f"{base} Context: {context}" if context else base

