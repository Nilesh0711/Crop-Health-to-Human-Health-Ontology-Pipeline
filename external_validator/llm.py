"""Strict JSON clients for the legacy local and supported OpenCode LLM paths."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Any

from .config import Settings


class LLMError(RuntimeError):
    pass


class OllamaClient:
    """Minimal Ollama API client; Qwen remains local to the workstation."""

    def __init__(self, settings: Settings, model: str | None = None):
        self.settings = settings
        self.model = model or settings.model

    def health_check(self) -> dict[str, Any]:
        return self._request("/api/tags", None, method="GET", timeout_seconds=5)

    def chat_json(self, system: str, user: str, *, max_tokens: int = 160,
                  num_ctx: int | None = None, json_schema: dict[str, Any] | None = None) -> dict[str, Any]:
        # Ollama keeps one runner for a loaded model. Use one context size for
        # extraction, semantic checks, and article judging so changing stages
        # does not force model runner reloads.
        payload = {"model": self.model, "stream": False, "think": False,
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


class OpenCodeClient:
    """JSON-only extraction client backed by the locally authenticated OpenCode CLI.

    OpenCode owns its provider credentials, so this client never reads, copies,
    or persists them.  The CLI returns JSONL events; only its final text event
    is treated as the model response.
    """

    def __init__(self, settings: Settings, model: str | None = None):
        self.settings = settings
        self.model = model or settings.extraction_model
        self._metrics_lock = threading.Lock()
        self._request_count = 0
        self._request_seconds = 0.0

    @property
    def request_metrics(self) -> dict[str, float | int]:
        with self._metrics_lock:
            return {"requests": self._request_count, "elapsed_seconds": round(self._request_seconds, 3)}

    def health_check(self) -> dict[str, Any]:
        command = shutil.which(self.settings.opencode_command)
        if not command:
            raise LLMError(f"OpenCode command not found: {self.settings.opencode_command}")
        try:
            result = subprocess.run(
                [command, "models"], capture_output=True, text=True,
                timeout=20, check=False, encoding="utf-8", errors="replace",
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise LLMError(f"OpenCode model listing failed: {exc}") from exc
        if result.returncode != 0:
            raise LLMError(f"OpenCode model listing failed: {result.stderr.strip()[:500]}")
        models = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        return {"models": models}

    def chat_json(self, system: str, user: str, *, max_tokens: int = 160,
                  num_ctx: int | None = None, json_schema: dict[str, Any] | None = None) -> dict[str, Any]:
        del max_tokens, num_ctx  # OpenCode's provider selects generation/context limits.
        command = shutil.which(self.settings.opencode_command)
        if not command:
            raise LLMError(f"OpenCode command not found: {self.settings.opencode_command}")
        schema_instruction = (
            "Return exactly one JSON object matching this JSON Schema. Do not use Markdown, tools, "
            "or commentary. JSON Schema:\n" + json.dumps(json_schema, ensure_ascii=False)
            if json_schema else "Return exactly one JSON object. Do not use Markdown, tools, or commentary."
        )
        prompt = f"SYSTEM INSTRUCTIONS:\n{system}\n\nUSER INPUT:\n{user}\n\n{schema_instruction}"
        # Passing a 4,000-word packet as a Windows command-line argument can
        # exceed CreateProcess' argument limit. Attach it transiently instead.
        prompt_path = ""
        started_at = time.monotonic()
        try:
            with tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".txt", delete=False) as handle:
                handle.write(prompt)
                prompt_path = handle.name
            result = subprocess.run(
                [command, "run", "--model", self.model, "--format", "json",
                 "--file", prompt_path, "Read the attached instruction-and-input file and follow it exactly."],
                capture_output=True, text=True, timeout=self.settings.request_timeout_seconds,
                check=False, encoding="utf-8", errors="replace",
            )
        except subprocess.TimeoutExpired as exc:
            raise LLMError(f"OpenCode request for {self.model} timed out after {self.settings.request_timeout_seconds}s") from exc
        except OSError as exc:
            raise LLMError(f"OpenCode request for {self.model} failed: {exc}") from exc
        finally:
            with self._metrics_lock:
                self._request_count += 1
                self._request_seconds += time.monotonic() - started_at
            if prompt_path:
                try:
                    os.unlink(prompt_path)
                except OSError:
                    pass
        if result.returncode != 0:
            detail = (result.stderr or result.stdout).strip().replace("\n", " ")[:600]
            raise LLMError(f"OpenCode request for {self.model} failed: {detail}")
        content = self._final_text_event(result.stdout)
        return OllamaClient._parse_json_response(content)

    @staticmethod
    def _final_text_event(output: str) -> str:
        texts: list[str] = []
        for line in output.splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if event.get("type") == "text":
                text = event.get("part", {}).get("text")
                if isinstance(text, str) and text.strip():
                    texts.append(text)
        if not texts:
            raise LLMError("OpenCode returned no text response")
        return "".join(texts)
