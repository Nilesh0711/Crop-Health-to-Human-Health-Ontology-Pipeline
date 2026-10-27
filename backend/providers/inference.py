"""Direct OpenCode inference, with fixed provider routes and no hidden retries."""
import re
import asyncio
from contextlib import suppress
import uuid

import httpx

from backend.providers.credentials import api_key
from backend.shared.cancellation import current_token, check_cancelled


class InferenceError(RuntimeError):
    def __init__(self, message, status='failed'):
        super().__init__(message)
        self.status = status


def _route(selector):
    provider, separator, model = selector.partition('/')
    if not separator or provider != 'opencode-go' or not model or '#' in model:
        raise InferenceError('Direct inference requires an opencode-go/ model selector without a CLI variant')
    base = 'https://opencode.ai/inference/go'
    return provider, model, base


class InferenceHTTP:
    def __init__(self, settings, thinking_enabled=None):
        self.settings = settings
        self.thinking_enabled = thinking_enabled
        self.session_id = 'crop-health-' + uuid.uuid4().hex

    def _headers(self):
        key = api_key()
        if not key:
            raise InferenceError('OpenCode API key is missing. Configure Windows Credential Manager or OPENCODE_API_KEY.')
        return {'Authorization': 'Bearer ' + key, 'User-Agent': self.settings.user_agent,
                'x-opencode-session': self.session_id}

    async def _request_during_job(self, token, method, url, headers, timeout, kwargs):
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            task = asyncio.create_task(client.request(method, url, headers=headers, **kwargs))
            try:
                while not task.done():
                    token.check()
                    await asyncio.wait({task}, timeout=0.1)
                token.check()
                return await task
            finally:
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                elif not task.cancelled():
                    task.exception()  # Retrieve a simultaneous transport failure on cancellation.

    def _request(self, method, url, **kwargs):
        check_cancelled()
        try:
            timeout = min(20, self.settings.request_timeout_seconds) if method == 'GET' else self.settings.request_timeout_seconds
            headers = self._headers()
            token = current_token()
            if token:
                response = asyncio.run(self._request_during_job(token, method, url, headers, timeout, kwargs))
            else:
                with httpx.Client(timeout=timeout, follow_redirects=False) as client:
                    response = client.request(method, url, headers=headers, **kwargs)
        except httpx.TimeoutException:
            raise InferenceError(f'Request timed out after {self.settings.request_timeout_seconds}s', 'timeout') from None
        except httpx.RequestError as exc:
            # HTTP client exceptions can contain request details; never log them.
            raise InferenceError(f'OpenCode inference connection failed ({type(exc).__name__})') from None
        if response.status_code != 200:
            hints = {400: 'Request parameters or response format were rejected',
                     401: 'API key was rejected', 402: 'Billing or usage limit requires attention',
                     403: 'API access was denied', 404: 'Model or endpoint was not found',
                     429: 'Rate or usage limit reached'}
            hint = hints.get(response.status_code, 'Provider request failed')
            try:
                error = response.json().get('error', {})
                message = error.get('message') if isinstance(error, dict) else None
                if isinstance(message, str):
                    message = message.replace(headers['Authorization'][7:], '[redacted]')
                    message = re.sub(r'(?i)Bearer\s+\S+|oc_sk_\S+', '[redacted]', message)
                    hint += '; ' + ' '.join(message.split())[:350]
            except (ValueError, AttributeError):
                pass
            raise InferenceError(f'OpenCode inference HTTP {response.status_code}: {hint}; no automatic retry or model substitution')
        try:
            data = response.json()
        except ValueError:
            raise InferenceError('OpenCode inference returned an invalid API response envelope') from None
        if not isinstance(data, dict):
            raise InferenceError('OpenCode inference returned a non-object API response envelope')
        return data

    def models(self):
        models = []
        prefixes = {selector.partition('/')[0] for selector in self.settings.task_models.values()}
        for provider in sorted(prefixes):
            _, _, base = _route(provider + '/catalog')
            data = self._request('GET', base + '/v1/models')
            models.extend(provider + '/' + row['id'] for row in data.get('data', [])
                          if isinstance(row, dict) and isinstance(row.get('id'), str))
        return {'models': models, 'transport': 'api'}

    def chat(self, selector, system, user, schema, output_limit):
        _, model, base = _route(selector)
        mode = self.settings.opencode_response_format
        if mode == 'model_default':
            # Configuration probes: MiMo accepts json_schema; DeepSeek rejects
            # it with HTTP 400. This selection is explicit, never a retry.
            mode = 'json_schema' if model in {'mimo-v2.6-flash', 'mimo-v2.6-pro'} else 'json_object'
        response_format = {'type': 'json_object'}
        if mode == 'json_schema' and schema:
            response_format = {'type': 'json_schema', 'json_schema': {
                'name': 'pipeline_response', 'strict': True, 'schema': schema}}
        payload = {'model': model, 'messages': [{'role': 'system', 'content': system},
                                               {'role': 'user', 'content': user}],
                   'temperature': 0, 'max_tokens': output_limit, 'stream': False,
                   'response_format': response_format}
        if self.thinking_enabled is not None:
            payload['thinking'] = {'type': 'enabled' if self.thinking_enabled else 'disabled'}
        data = self._request('POST', base + '/openai/v1/chat/completions', json=payload)
        choices = data.get('choices')
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise InferenceError('OpenCode inference returned no unique model answer')
        choice = choices[0]
        message = choice.get('message') or {}
        content = message.get('content')
        if message.get('refusal'):
            raise InferenceError('The selected model refused this request')
        if content is None:
            content = ''  # Let the recovery coordinator regenerate a missing answer.
        if not isinstance(content, str):
            raise InferenceError('The selected model returned an unsupported final-answer format')
        return {'content': content, 'finish_reason': choice.get('finish_reason'),
                'usage': data.get('usage') or {}, 'provider_request_id': data.get('id'),
                'resolved_model': data.get('model'), 'response_format': response_format['type']}


def normalize_usage(usage):
    def integer(value):
        return value if type(value) is int and value >= 0 else None
    prompt, completion = integer(usage.get('prompt_tokens')), integer(usage.get('completion_tokens'))
    details = usage.get('completion_tokens_details') or {}
    reasoning = integer(details.get('reasoning_tokens', usage.get('reasoning_tokens')))
    cached = integer((usage.get('prompt_tokens_details') or {}).get('cached_tokens', usage.get('prompt_cache_hit_tokens')))
    reported = prompt is not None and completion is not None
    return {'usage_reported': reported, 'input_tokens_reported': prompt is not None,
            'input_tokens': prompt or 0,
            'output_tokens': max(0, (completion or 0) - (reasoning or 0)),
            'reasoning_tokens': reasoning or 0, 'reasoning_usage_reported': reasoning is not None,
            'cache_read_tokens': cached or 0, 'cache_write_tokens': 0,
            'model_steps': 1 if reported else 0}
