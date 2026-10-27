"""Batch model requests while reusing each existing validator's post-checks."""

from __future__ import annotations

import json
from backend.shared.cancellation import check_cancelled
from collections import Counter
from typing import Any, Callable, Iterator
from backend.providers.llm import LLMServiceError


class _RequestCaptured(Exception):
    def __init__(self, system: str, user: str, options: dict[str, Any]):
        self.system, self.user, self.options = system, user, options


class _CaptureClient:
    def __init__(self, client: Any):
        self.settings = client.settings

    def chat_json(self, system: str, user: str, **options: Any) -> dict[str, Any]:
        raise _RequestCaptured(system, user, options)


class _ReplayClient:
    def __init__(self, client: Any, response: dict[str, Any]):
        self.settings, self.response = client.settings, response

    def chat_json(self, *_args: Any, **_options: Any) -> dict[str, Any]:
        return self.response


def iter_batched_judgments(candidates: list[dict[str, Any]], client: Any,
                          validator: Callable[..., dict[str, Any]], batch_size: int = 3,
                          progress: Callable[[str], None] | None = None,
                          label: str = "Validation") -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """One model request per small batch; no retry or cross-claim evidence sharing.

    Capture only the model-request boundary, then replay each answer through
    the original validator. Deterministic evidence rules stay identical for
    single and batched operation. Missing/duplicate IDs affect only that row.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    for start in range(0, len(candidates), batch_size):
        check_cancelled()
        batch = candidates[start:start + batch_size]
        if progress:
            progress(f"{label} batch {start // batch_size + 1}: {len(batch)} claim(s)")
        immediate: dict[str, dict[str, Any]] = {}
        requests: list[tuple[dict[str, Any], _RequestCaptured]] = []
        for candidate in batch:
            try:
                immediate[candidate["candidate_id"]] = validator(candidate, _CaptureClient(client))
            except _RequestCaptured as captured:
                requests.append((candidate, captured))
        answers: dict[str, dict[str, Any]] = {}
        error = "Missing or duplicate candidate ID in batch response"
        if requests:
            first = requests[0][1]
            if any(request.system != first.system or request.options.get("json_schema") != first.options.get("json_schema")
                   for _, request in requests):
                raise ValueError("A batch must use the same validation task and response schema")
            item_schema = json.loads(json.dumps(first.options["json_schema"]))
            item_schema["properties"]["candidate_id"] = {"type": "string"}
            item_schema["required"] = list(dict.fromkeys([*item_schema.get("required", []), "candidate_id"]))
            schema = {"type": "object", "properties": {"results": {"type": "array", "items": item_schema,
                      "minItems": len(requests), "maxItems": len(requests)}},
                      "required": ["results"], "additionalProperties": False}
            inputs = []
            for candidate, request in requests:
                try:
                    payload = json.loads(request.user)
                except json.JSONDecodeError:
                    payload = request.user
                inputs.append({"candidate_id": candidate["candidate_id"], "input": payload})
            system = (first.system + '\nProcess the supplied items INDEPENDENTLY. Return {"results":[...]} with '
                      "exactly one result per candidate_id, using only that item's evidence. Never borrow facts, "
                      "entity identities, or qualifiers from another item. Keep each rationale under 250 characters.")
            def check_batch_response(response):
                from backend.providers.structured_output import IncompleteResponseError
                rows = response.get('results', [])
                expected = sorted(candidate['candidate_id'] for candidate, _ in requests)
                actual = sorted(str(row.get('candidate_id')) for row in rows)
                if actual != expected:
                    raise IncompleteResponseError('Batch response does not identify each supplied claim exactly once')
                checks = {candidate['candidate_id']: request.options.get('response_check') for candidate, request in requests}
                for row in rows:
                    if checks[row['candidate_id']]:
                        checks[row['candidate_id']](row)
            try:
                response = client.chat_json(system, json.dumps({"items": inputs}, ensure_ascii=False),
                                            max_tokens=sum(request.options.get("max_tokens", 520) for _, request in requests),
                                            json_schema=schema,
                                            response_check=check_batch_response,
                                            request_label=f"{label} batch: {', '.join(candidate['candidate_id'] for candidate, _ in requests)}")
                rows = response.get("results", [])
                rows = rows if isinstance(rows, list) else []
                counts = Counter(row.get("candidate_id") for row in rows
                                 if isinstance(row, dict) and isinstance(row.get("candidate_id"), str))
                answers = {row["candidate_id"]: row for row in rows if isinstance(row, dict)
                           and isinstance(row.get("candidate_id"), str) and counts[row["candidate_id"]] == 1}
            except LLMServiceError:
                raise
            except Exception as exc:
                error = f"{label} batch request failed: {exc}"
        for candidate in batch:
            identifier = candidate["candidate_id"]
            if identifier in immediate:
                yield candidate, immediate[identifier]
            elif identifier in answers:
                try:
                    yield candidate, validator(candidate, _ReplayClient(client, answers[identifier]))
                except Exception as exc:
                    yield candidate, {"candidate_id": identifier, "decision": "needs_review", "action": "review",
                                      "rationale": f"{label} response could not be validated: {exc}", "batch_error": True}
            else:
                yield candidate, {"candidate_id": identifier, "decision": "needs_review", "action": "review",
                                  "rationale": error, "batch_error": True}
