"""
DAP Launcher – Sprint 2 of FlowDelta.

Manages the full lifecycle of a ``debugpy`` subprocess so callers do not
need to start the server manually:

  1. Spawns  ``python -m debugpy --listen HOST:PORT --wait-for-client SCRIPT``
  2. Polls the TCP port until ``debugpy`` is ready (with configurable timeout)
  3. Connects a :class:`DAPClient`, calls ``initialize()``, and returns it
  4. On exit, disconnects the client and terminates the subprocess

Also supports **attach mode** – connects to an already-running process by
PID without spawning a new subprocess.

Example (script mode)::

    async with DAPLauncher("src/app.py", breakpoints={"src/app.py": [10, 42]}) as client:
        await client.configuration_done()
        async for snapshot in client.iter_breakpoint_hits():
            print(snapshot.to_dict())

Example (attach mode)::

    async with DAPLauncher.attach(pid=12345) as client:
        ...
"""

from __future__ import annotations

import asyncio
import logging
import socket
import subprocess
import sys
import time
from typing import Dict, List, Optional

from .dap_client import DAPClient

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "127.0.0.1"
_DEFAULT_PORT = 5679          # different from IDE default (5678) to avoid conflicts


class DAPLauncher:
    """
    Launches a ``debugpy`` subprocess and wraps it in a :class:`DAPClient`.

    Parameters
    ----------
    script : str
        Path to the Python script to debug.
    script_args : list[str]
        Arguments to pass to *script*.
    host : str
        Hostname for ``debugpy`` to listen on.
    port : int
        Port for ``debugpy`` to listen on.
    python : str
        Python executable to use (defaults to the current interpreter).
    ready_timeout : float
        Seconds to wait for ``debugpy`` to open the port.
    breakpoints : dict[str, list[int]] | None
        Convenience parameter – breakpoints to set after connecting.
        Keys are file paths, values are lists of line numbers.
    """

    def __init__(
        self,
        script: str,
        script_args: Optional[List[str]] = None,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        python: str = sys.executable,
        ready_timeout: float = 15.0,
        breakpoints: Optional[Dict[str, List[int]]] = None,
    ) -> None:
        self.script = script
        self.script_args = script_args or []
        self.host = host
        self.port = port
        self.python = python
        self.ready_timeout = ready_timeout
        self.breakpoints = breakpoints or {}

        self._proc: Optional[subprocess.Popen] = None
        self._client: Optional[DAPClient] = None
        self._attach_pid: Optional[int] = None

    # ------------------------------------------------------------------
    # Alternate constructor: attach to existing process
    # ------------------------------------------------------------------

    @classmethod
    def attach(
        cls,
        pid: int,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        ready_timeout: float = 10.0,
    ) -> "DAPLauncher":
        """
        Return a :class:`DAPLauncher` that attaches to an existing process
        by *pid* instead of spawning a new one.

        The target process must already have ``debugpy`` listening on *port*.
        """
        launcher = cls.__new__(cls)
        launcher.script = ""
        launcher.script_args = []
        launcher.host = host
        launcher.port = port
        launcher.python = sys.executable
        launcher.ready_timeout = ready_timeout
        launcher.breakpoints = {}
        launcher._proc = None
        launcher._client = None
        launcher._attach_pid = pid
        return launcher

    # ------------------------------------------------------------------
    # Async context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> DAPClient:
        if self._attach_pid is not None:
            return await self._connect_attach()
        return await self._launch_and_connect()

    async def __aexit__(self, *_) -> None:
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
        if self._proc:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            logger.info("debugpy subprocess terminated (pid=%s)", self._proc.pid)

    # ------------------------------------------------------------------
    # Launch mode
    # ------------------------------------------------------------------

    async def _launch_and_connect(self) -> DAPClient:
        cmd = [
            self.python, "-m", "debugpy",
            "--listen", f"{self.host}:{self.port}",
            "--wait-for-client",
            self.script,
            *self.script_args,
        ]
        logger.info("Launching debugpy: %s", " ".join(cmd))
        self._proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        await self._wait_for_port()
        return await self._connect()

    # ------------------------------------------------------------------
    # Attach mode
    # ------------------------------------------------------------------

    async def _connect_attach(self) -> DAPClient:
        await self._wait_for_port()
        client = await self._connect()
        await client.attach(self._attach_pid)  # type: ignore[arg-type]
        return client

    # ------------------------------------------------------------------
    # Shared connect + setup
    # ------------------------------------------------------------------

    async def _connect(self) -> DAPClient:
        self._client = DAPClient(host=self.host, port=self.port)
        await self._client.connect()
        await self._client.initialize()
        for filepath, lines in self.breakpoints.items():
            await self._client.set_breakpoints(filepath, lines)
        logger.info(
            "DAPClient connected to %s:%s (%d breakpoint file(s))",
            self.host, self.port, len(self.breakpoints),
        )
        return self._client

    # ------------------------------------------------------------------
    # Port readiness poll
    # ------------------------------------------------------------------

    async def _wait_for_port(self) -> None:
        """Poll until the TCP port accepts connections or timeout."""
        deadline = time.monotonic() + self.ready_timeout
        while time.monotonic() < deadline:
            if self._port_open():
                logger.debug("debugpy port %s:%s is ready", self.host, self.port)
                return
            await asyncio.sleep(0.25)
        raise TimeoutError(
            f"debugpy did not open {self.host}:{self.port} within "
            f"{self.ready_timeout}s"
        )

    def _port_open(self) -> bool:
        try:
            with socket.create_connection((self.host, self.port), timeout=0.5):
                return True
        except OSError:
            return False

    # ------------------------------------------------------------------
    # Convenience: run a script and collect all snapshots
    # ------------------------------------------------------------------

    @staticmethod
    async def run_and_capture(
        script: str,
        breakpoints: Dict[str, List[int]],
        script_args: Optional[List[str]] = None,
        host: str = _DEFAULT_HOST,
        port: int = _DEFAULT_PORT,
        ready_timeout: float = 15.0,
        event_timeout: float = 60.0,
    ) -> list:
        """
        High-level helper: launch *script*, set breakpoints, collect all
        :class:`StateSnapshot` objects, and return them as a list.

        Example::

            snapshots = await DAPLauncher.run_and_capture(
                "src/app.py",
                breakpoints={"src/app.py": [10, 25, 42]},
            )
        """
        from ..state_tracker.trace_recorder import FlowTrace  # avoid circular
        snapshots = []
        launcher = DAPLauncher(
            script=script,
            script_args=script_args,
            host=host,
            port=port,
            ready_timeout=ready_timeout,
            breakpoints=breakpoints,
        )
        async with launcher as client:
            await client.configuration_done()
            async for snapshot in client.iter_breakpoint_hits():
                snapshots.append(snapshot)
        return snapshots
