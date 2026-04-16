"""Shared lifecycle management for language-specific DAP launchers."""

from __future__ import annotations

import asyncio
import subprocess
from typing import Optional

from ..state_tracker.dap_client import DAPClient


class BaseDAPLauncher:
    """
    Common async context manager for DAP launcher subclasses.
    """

    DEFAULT_HOST = "127.0.0.1"
    DEFAULT_PORT = 5678
    CONNECT_TIMEOUT = 15

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        connect_timeout: int = CONNECT_TIMEOUT,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._proc: Optional[subprocess.Popen] = None
        self._client: Optional[DAPClient] = None

    async def __aenter__(self) -> DAPClient:
        self._proc = await self._start_server()
        self._client = await self._connect()
        return self._client

    async def __aexit__(self, *_) -> None:
        if self._client:
            try:
                await self._client.__aexit__(None, None, None)
            except Exception:
                pass
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()

    async def _start_server(self) -> subprocess.Popen:
        raise NotImplementedError

    async def _connect(self) -> DAPClient:
        """Poll until the DAP server accepts connections, then return client."""
        import time
        deadline = time.monotonic() + self.connect_timeout
        while time.monotonic() < deadline:
            try:
                client = DAPClient(self.host, self.port)
                await client.__aenter__()
                return client
            except (ConnectionRefusedError, OSError):
                await asyncio.sleep(0.25)
        raise TimeoutError(
            f"DAP server at {self.host}:{self.port} did not start within "
            f"{self.connect_timeout}s"
        )
