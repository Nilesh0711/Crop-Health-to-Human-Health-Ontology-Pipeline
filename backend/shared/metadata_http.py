"""Cancellable scholarly metadata I/O, independent of inference credentials."""
import asyncio
from contextlib import suppress
import httpx
from backend.shared.cancellation import check_cancelled


class MetadataError(OSError):
    def __init__(self, status):
        self.status = status
        super().__init__(f'Metadata service unavailable ({status})')


def request_json(method, url, *, headers, timeout, max_bytes, **kwargs):
    async def receive():
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
            async def read():
                async with client.stream(method, url, headers=headers, **kwargs) as response:
                    response.raise_for_status()
                    body = bytearray()
                    async for part in response.aiter_bytes():
                        body.extend(part)
                        if len(body) > max_bytes:
                            raise ValueError('Metadata response exceeded the configured size budget')
                    import json
                    return json.loads(body)
            async def bounded_read():
                for attempt in range(2):
                    try:
                        return await read()
                    except httpx.HTTPStatusError as exc:
                        if attempt or exc.response.status_code not in {429, 500, 502, 503, 504}:
                            raise
                        await asyncio.sleep(0.6)
            task = asyncio.create_task(bounded_read())
            try:
                while not task.done():
                    check_cancelled()
                    await asyncio.wait({task}, timeout=0.1)
                check_cancelled()
                return await task
            finally:
                if not task.done():
                    task.cancel()
                    with suppress(asyncio.CancelledError):
                        await task
                elif not task.cancelled():
                    task.exception()
    try:
        return asyncio.run(receive())
    except httpx.HTTPError as exc:
        # Do not expose authorization headers, query text or account details.
        status = exc.response.status_code if isinstance(exc, httpx.HTTPStatusError) else type(exc).__name__
        raise MetadataError(status) from None
