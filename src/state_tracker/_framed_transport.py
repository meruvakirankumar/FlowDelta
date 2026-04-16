"""Shared Content-Length framed message transport for DAP and LSP protocols."""

from __future__ import annotations

import asyncio
import json
from typing import Optional


class FramedTransport:
    """
    Base class for Content-Length framed message protocols (DAP, LSP).

    Subclasses provide reader/writer via ``_get_reader()`` / ``_get_writer()``.
    """

    def _get_reader(self) -> asyncio.StreamReader:
        raise NotImplementedError

    def _get_writer(self):
        raise NotImplementedError

    async def send_raw(self, body: bytes) -> None:
        writer = self._get_writer()
        header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
        writer.write(header + body)
        await writer.drain()

    async def recv_raw(self, timeout: Optional[float] = None) -> Optional[bytes]:
        """Read one Content-Length framed message. Returns body bytes or None."""
        reader = self._get_reader()
        header_bytes = b""
        while True:
            if timeout is not None:
                line = await asyncio.wait_for(reader.readline(), timeout=timeout)
            else:
                line = await reader.readline()
            if line in (b"\r\n", b"\n", b""):
                break
            header_bytes += line

        content_length = 0
        for part in header_bytes.split(b"\r\n"):
            if part.lower().startswith(b"content-length:"):
                content_length = int(part.split(b":", 1)[1].strip())

        if content_length == 0:
            return None

        if timeout is not None:
            body = await asyncio.wait_for(
                reader.readexactly(content_length), timeout=timeout
            )
        else:
            body = await reader.readexactly(content_length)
        return body

    async def send_json(self, obj: dict) -> None:
        await self.send_raw(json.dumps(obj).encode("utf-8"))

    async def recv_json(self, timeout: Optional[float] = None) -> Optional[dict]:
        body = await self.recv_raw(timeout=timeout)
        if body is None:
            return None
        return json.loads(body.decode("utf-8"))
