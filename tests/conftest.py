"""The test suite must never spend real API credits or read a real API key."""
import httpx
import pytest


@pytest.fixture(autouse=True)
def isolate_inference_network(monkeypatch):
    def blocked(*args, **kwargs):
        raise AssertionError('Unexpected live HTTP request in an offline test; use MockTransport')
    async def blocked_async(*args, **kwargs):
        raise AssertionError('Unexpected live asynchronous HTTP request in an offline test')
    monkeypatch.setattr(httpx.HTTPTransport, 'handle_request', blocked)
    monkeypatch.setattr(httpx.AsyncHTTPTransport, 'handle_async_request', blocked_async)
    monkeypatch.setattr('backend.providers.inference.api_key', lambda: 'test-only-key')
