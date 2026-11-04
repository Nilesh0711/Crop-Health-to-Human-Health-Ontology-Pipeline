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


def iter_parallel_judgments(candidates, client, validator, batch_size=3, progress=None,
                            label='Validation', *, keyed=False, workers=2):
    """Bounded independent batches; persist ready findings on the owner thread."""
    if workers == 1:
        yield from iter_batched_judgments(candidates, client, validator, batch_size, progress, label, keyed=keyed)
        return
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from copy import deepcopy
    from backend.shared.cancellation import CancellationToken, cancellation_scope, current_token, submit_work
    token = CancellationToken(current_token())
    originals = {candidate['candidate_id']:candidate for candidate in candidates}
    def assess(batch):
        return list(iter_batched_judgments(deepcopy(batch), client, validator, batch_size, None, label, keyed=keyed))
    with cancellation_scope(token):
        with ThreadPoolExecutor(max_workers=workers, thread_name_prefix='source-batch') as executor:
            futures = [submit_work(executor, assess, candidates[i:i+batch_size])
                       for i in range(0, len(candidates), batch_size)]
            try:
                for future in as_completed(futures):
                    check_cancelled()
                    for candidate,result in future.result():
                        # Only the owner publishes completed corrections. Other
                        # workers must not change a claim in an unrelated snapshot.
                        original = originals[candidate['candidate_id']]
                        original.clear()
                        original.update(candidate)
                        yield original,result
            except BaseException:
                token.request()
                for future in futures:
                    future.cancel()
                raise


def iter_batched_judgments(candidates: list[dict[str, Any]], client: Any,
                          validator: Callable[..., dict[str, Any]], batch_size: int = 3,
                          progress: Callable[[str], None] | None = None,
                          label: str = "Validation", *, keyed: bool = False) -> Iterator[tuple[dict[str, Any], dict[str, Any]]]:
    """One model request per small batch; no retry or cross-claim evidence sharing.

    Capture only the model-request boundary, then replay each answer through
    the original validator. Deterministic evidence rules stay identical for
    single and batched operation. Missing/duplicate IDs affect only that row.
    The source pipeline opts into keyed identities: one assessment per short C
    key, with exact repeated section context shared through source_contexts.
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
            aliases = {f'C{n}': candidate['candidate_id'] for n, (candidate, _) in enumerate(requests, 1)}
            if keyed:
                schema = {'type': 'object', '$defs': {'assessment': item_schema},
                    'properties': {'results': {'type': 'object', 'properties': {
                        alias: {'$ref': '#/$defs/assessment'} for alias in aliases},
                        'required': list(aliases), 'additionalProperties': False}},
                    'required': ['results'], 'additionalProperties': False}
            else:
                item_schema["properties"]["candidate_id"] = {"type": "string"}
                item_schema["required"] = list(dict.fromkeys([*item_schema.get("required", []), "candidate_id"]))
                schema = {"type": "object", "properties": {"results": {"type": "array", "items": item_schema,
                          "minItems": len(requests), "maxItems": len(requests)}},
                          "required": ["results"], "additionalProperties": False}
            inputs = []
            contexts, context_refs = {}, {}
            for alias, (candidate, request) in zip(aliases, requests):
                try:
                    payload = json.loads(request.user)
                except json.JSONDecodeError:
                    payload = request.user
                if keyed and isinstance(payload, dict) and isinstance(payload.get('nearby_context'), str):
                    context = payload.pop('nearby_context')
                    if context not in context_refs:
                        ref = f'T{len(contexts) + 1}'
                        context_refs[context] = ref
                        contexts[ref] = context
                    payload['nearby_context_ref'] = context_refs[context]
                inputs.append({**({'item_id': alias} if keyed else {'candidate_id': candidate['candidate_id']}), 'input': payload})
            system = (first.system + '\nProcess the supplied items INDEPENDENTLY. ' +
                      ('Return {"results":{"C1":{...},"C2":{...}}} using exactly the supplied item_id keys once each. '
                       'Each value is ONLY the assessment object defined by $defs.assessment; never add candidate_id '
                       'or item_id inside it, and never return a results array. The response assessment keys are: '
                       + ', '.join(item_schema['properties']) + '. '
                       'nearby_context_ref resolves to the exact original source text in source_contexts. '
                       'The shared text is not a summary; use only the reference attached to that item. '
                       if keyed else 'Return {"results":[...]} with ') +
                      "exactly one result per supplied item, using only that item's evidence. Never borrow facts, "
                      "entity identities, or qualifiers from another item. Keep each rationale under 250 characters.")
            def check_batch_response(response):
                from backend.providers.structured_output import IncompleteResponseError
                rows = response.get('results', [])
                if keyed:
                    if not isinstance(rows, dict) or set(rows) != set(aliases):
                        raise IncompleteResponseError('Batch response must identify each supplied C item exactly once')
                    rows = [{'candidate_id': aliases[alias], **row} for alias, row in rows.items()]
                expected = sorted(candidate['candidate_id'] for candidate, _ in requests)
                actual = sorted(str(row.get('candidate_id')) for row in rows)
                if actual != expected:
                    raise IncompleteResponseError('Batch response does not identify each supplied claim exactly once')
                checks = {candidate['candidate_id']: request.options.get('response_check') for candidate, request in requests}
                for row in rows:
                    if checks[row['candidate_id']]:
                        checks[row['candidate_id']](row)
            try:
                response = client.chat_json(system, json.dumps({"items": inputs, **({'source_contexts': contexts} if keyed else {})}, ensure_ascii=False),
                                            max_tokens=sum(request.options.get("max_tokens", 520) for _, request in requests),
                                            json_schema=schema,
                                            response_check=check_batch_response,
                                            request_label=f"{label} batch: {', '.join(candidate['candidate_id'] for candidate, _ in requests)}")
                rows = response.get("results", [])
                if keyed and isinstance(rows, dict):
                    rows = [{'candidate_id': aliases[alias], **row} for alias, row in rows.items() if alias in aliases and isinstance(row, dict)]
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
