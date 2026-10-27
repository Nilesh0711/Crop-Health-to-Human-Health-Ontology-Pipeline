"""Deterministic research-evaluation sampling and metric helpers."""

from __future__ import annotations

import json
import math
from collections import defaultdict
from pathlib import Path
from typing import Any

from backend.shared.storage import stable_hash, write_jsonl


def _predicted_status(candidate: dict[str, Any], validation: dict[str, Any] | None) -> str:
    if candidate.get("schema_error"):
        return "schema_rejected"
    semantic = (candidate.get("semantic_validation") or {}).get("decision")
    if semantic not in {"accepted", "not_run"}:
        return f"semantic_{semantic or 'pending'}"
    source = (candidate.get("source_validation") or {}).get("decision")
    if source != "entailed":
        return f"source_{source or 'pending'}"
    return str((validation or {}).get("validation_status") or "external_pending")


def evaluation_sample(candidates: list[dict[str, Any]], validations: list[dict[str, Any]],
                      limit: int = 80) -> list[dict[str, Any]]:
    """Create a reproducible status-stratified annotation file.

    Model predictions are copied separately from empty gold fields so pipeline
    output can never be mistaken for ground truth.
    """
    by_id = {item.get("candidate_id"): item for item in validations}
    strata: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for candidate in candidates:
        validation = by_id.get(candidate.get("candidate_id"))
        status = _predicted_status(candidate, validation)
        documents = [document for attempt in (validation or {}).get("attempts", [])
                     for document in attempt.get("evidence_documents", [])]
        record = {
            "candidate_id": candidate.get("candidate_id"),
            "claim": {key: candidate.get(key) for key in
                      ("subject", "relation", "object", "subject_type", "object_type", "certainty", "qualifiers")},
            "source_evidence_span": candidate.get("source_evidence_span"),
            "prediction": {
                "status": status,
                "semantic": (candidate.get("semantic_validation") or {}).get("decision"),
                "source_entailment": (candidate.get("source_validation") or {}).get("decision"),
                "external": (validation or {}).get("validation_status"),
                "retrieved_document_ids": [item.get("evidence_document_id") for item in documents],
            },
            "gold": {
                "valid_triple": None,
                "subject_type": None,
                "object_type": None,
                "relation": None,
                "source_entailment": None,
                "external_label": None,
                "relevant_evidence_document_ids": [],
                "reviewer": None,
                "notes": None,
            },
        }
        strata[status].append(record)
    for records in strata.values():
        records.sort(key=lambda item: stable_hash(str(item["candidate_id"]), 32))
    selected: list[dict[str, Any]] = []
    while len(selected) < limit and any(strata.values()):
        for status in sorted(strata):
            if strata[status] and len(selected) < limit:
                selected.append(strata[status].pop(0))
    return selected


def write_evaluation_sample(output_dir: Path, candidates: list[dict[str, Any]],
                            validations: list[dict[str, Any]], limit: int = 80,
                            preserve_existing_labels: bool = False) -> Path:
    path = output_dir / "evaluation_sample.jsonl"
    previous = {row['candidate_id']: row for row in read_jsonl(path)} if preserve_existing_labels and path.exists() else {}
    rows = evaluation_sample(candidates, validations, max(limit, len(previous)))
    for row in rows:
        old = previous.get(row['candidate_id'])
        if (old and old.get('claim') == row.get('claim')
                and old.get('source_evidence_span') == row.get('source_evidence_span')):
            row['gold'] = old['gold']
    write_jsonl(path, rows)
    return path


def _accuracy(pairs: list[tuple[Any, Any]]) -> float | None:
    return round(sum(predicted == gold for predicted, gold in pairs) / len(pairs), 4) if pairs else None


def _binary_metrics(pairs: list[tuple[bool, bool]]) -> dict[str, float | int | None]:
    if not pairs:
        return {"precision": None, "recall": None, "f1": None, "support": 0}
    true_positive = sum(predicted and gold for predicted, gold in pairs)
    false_positive = sum(predicted and not gold for predicted, gold in pairs)
    false_negative = sum(not predicted and gold for predicted, gold in pairs)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0.0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    return {"precision": round(precision, 4), "recall": round(recall, 4), "f1": round(f1, 4),
            "support": len(pairs)}


def score_evaluation(records: list[dict[str, Any]], retrieval_k: int = 3) -> dict[str, Any]:
    """Score completed gold fields without treating blank labels as negatives."""
    valid_pairs: list[tuple[Any, Any]] = []
    relation_pairs: list[tuple[Any, Any]] = []
    subject_type_pairs: list[tuple[Any, Any]] = []
    object_type_pairs: list[tuple[Any, Any]] = []
    source_pairs: list[tuple[Any, Any]] = []
    external_pairs: list[tuple[Any, Any]] = []
    recall_values: list[float] = []
    ndcg_values: list[float] = []
    for record in records:
        prediction, gold = record.get("prediction") or {}, record.get("gold") or {}
        claim = record.get("claim") or {}
        if gold.get("valid_triple") is not None:
            predicted_valid = prediction.get("semantic") in {"accepted", "not_run"} and prediction.get("source_entailment") == "entailed"
            valid_pairs.append((predicted_valid, gold["valid_triple"]))
        for target, pairs in (("relation", relation_pairs), ("subject_type", subject_type_pairs),
                              ("object_type", object_type_pairs)):
            if gold.get(target) is not None:
                pairs.append((claim.get(target), gold[target]))
        if gold.get("source_entailment") is not None:
            source_pairs.append((prediction.get("source_entailment"), gold["source_entailment"]))
        if gold.get("external_label") is not None:
            external_pairs.append((prediction.get("external"), gold["external_label"]))
        relevant = set(gold.get("relevant_evidence_document_ids") or [])
        if relevant:
            retrieved = list(prediction.get("retrieved_document_ids") or [])[:retrieval_k]
            recall_values.append(len(relevant & set(retrieved)) / len(relevant))
            dcg = sum((1.0 if identifier in relevant else 0.0) / math.log2(index + 2)
                      for index, identifier in enumerate(retrieved))
            ideal = sum(1.0 / math.log2(index + 2) for index in range(min(len(relevant), retrieval_k)))
            ndcg_values.append(dcg / ideal if ideal else 0.0)
    validity = _binary_metrics(valid_pairs)
    return {
        "labelled_records": sum(bool((record.get("gold") or {}).get("reviewer")) for record in records),
        "triple_validity_accuracy": _accuracy(valid_pairs),
        "triple_validity_precision": validity["precision"],
        "triple_validity_recall": validity["recall"],
        "triple_validity_f1": validity["f1"],
        "triple_validity_support": validity["support"],
        "relation_accuracy": _accuracy(relation_pairs),
        "subject_type_accuracy": _accuracy(subject_type_pairs),
        "object_type_accuracy": _accuracy(object_type_pairs),
        "source_entailment_accuracy": _accuracy(source_pairs),
        "external_label_accuracy": _accuracy(external_pairs),
        f"retrieval_recall@{retrieval_k}": round(sum(recall_values) / len(recall_values), 4) if recall_values else None,
        f"retrieval_ndcg@{retrieval_k}": round(sum(ndcg_values) / len(ndcg_values), 4) if ndcg_values else None,
    }


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]

