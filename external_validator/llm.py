"""Qwen/Ollama calls with strict JSON parsing and no hidden retries."""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from typing import Any

from .config import Settings


class LLMError(RuntimeError):
    pass


class OllamaClient:
    """Minimal Ollama API client; Qwen remains local to the workstation."""

    def __init__(self, settings: Settings):
        self.settings = settings

    def health_check(self) -> dict[str, Any]:
        return self._request("/api/tags", None, method="GET", timeout_seconds=5)

    def chat_json(self, system: str, user: str, *, max_tokens: int = 160,
                  num_ctx: int | None = None, json_schema: dict[str, Any] | None = None) -> dict[str, Any]:
        # Ollama keeps one runner for a loaded model. Use one context size for
        # extraction, semantic checks, and article judging so changing stages
        # does not force model runner reloads.
        payload = {"model": self.settings.model, "stream": False, "think": False,
            "format": json_schema or "json", "options": {"temperature": 0, "num_predict": max_tokens,
                "num_ctx": num_ctx or self.settings.context_tokens},
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        result = self._request("/api/chat", payload)
        if result.get("done_reason") == "length":
            raise LLMError("Qwen reached its output-token limit before completing the JSON response")
        content = result.get("message", {}).get("content", "")
        return self._parse_json_response(content)

    def _request(self, path: str, payload: dict[str, Any] | None, method: str = "POST",
                 timeout_seconds: int | None = None) -> dict[str, Any]:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = urllib.request.Request(f"{self.settings.ollama_url.rstrip('/')}{path}", data=data, method=method,
            headers={"Content-Type": "application/json; charset=utf-8"})
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds or self.settings.request_timeout_seconds) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(f"Local Qwen request failed: {exc}") from exc

    @staticmethod
    def _parse_json_response(content: str) -> dict[str, Any]:
        cleaned = content.strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", cleaned, flags=re.IGNORECASE)
        try:
            parsed = json.loads(cleaned)
        except json.JSONDecodeError as exc:
            # Some Ollama model templates prepend a harmless line before the
            # object. Accept one complete JSON object, but never invent or
            # repair an incomplete answer.
            start = cleaned.find("{")
            try:
                parsed, _ = json.JSONDecoder().raw_decode(cleaned[start:]) if start >= 0 else (None, 0)
            except json.JSONDecodeError:
                raise LLMError(f"Qwen returned non-JSON output: {content[:500]!r}") from exc
        if not isinstance(parsed, dict):
            raise LLMError("Qwen JSON response must be an object")
        return parsed
