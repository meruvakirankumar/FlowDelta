"""
DAP Client – Phase 2 of FlowDelta.

Implements a lightweight asyncio client for the Debug Adapter Protocol (DAP).
Works with ``debugpy`` running in server mode.

Typical usage::

    async with DAPClient("127.0.0.1", 5678) as client:
        await client.initialize()
        await client.set_breakpoints("src/app.py", [10, 25, 42])
        await client.launch("src/app.py")
        async for snapshot in client.iter_breakpoint_hits():
            print(snapshot)

Protocol reference: https://microsoft.github.io/debug-adapter-protocol/
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Dict, List, Optional

from ._framed_transport import FramedTransport

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------

@dataclass
class StackFrame:
    id: int
    name: str
    source: str
    line: int
    column: int


@dataclass
class StateSnapshot:
    """
    All variable state captured at one breakpoint hit.
    Includes the call stack and the local variables of each frame.
    """
    event: str               # "stopped" reason
    thread_id: int
    file: str
    line: int
    function: str
    stack: List[StackFrame] = field(default_factory=list)
    locals: Dict[str, Any] = field(default_factory=dict)
    sequence: int = 0        # monotonically increasing hit counter

    def to_dict(self) -> dict:
        return {
            "event": self.event,
            "thread_id": self.thread_id,
            "file": self.file,
            "line": self.line,
            "function": self.function,
            "stack": [
                {"id": f.id, "name": f.name, "source": f.source,
                 "line": f.line, "column": f.column}
                for f in self.stack
            ],
            "locals": self.locals,
            "sequence": self.sequence,
        }


# ---------------------------------------------------------------------------
# Low-level DAP transport
# ---------------------------------------------------------------------------

class _DAPTransport(FramedTransport):
    """Handles raw DAP message framing over a TCP socket."""

    def __init__(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self._reader = reader
        self._writer = writer
        self._seq = 0

    def _get_reader(self) -> asyncio.StreamReader:
        return self._reader

    def _get_writer(self):
        return self._writer

    async def send(self, message: dict) -> None:
        self._seq += 1
        message.setdefault("seq", self._seq)
        message.setdefault("type", "request")
        await self.send_json(message)

    async def recv(self) -> dict:
        result = await self.recv_json()
        return result or {}

    async def close(self) -> None:
        self._writer.close()
        try:
            await self._writer.wait_closed()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# DAP Client
# ---------------------------------------------------------------------------

class DAPClient:
    """
    Asyncio DAP client that attaches to a ``debugpy`` server.

    Parameters
    ----------
    host : str
        Hostname of the debugpy server.
    port : int
        Port of the debugpy server.
    connect_timeout : float
        Seconds to wait for the connection to succeed.
    """

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 5678,
        connect_timeout: float = 10.0,
    ) -> None:
        self.host = host
        self.port = port
        self.connect_timeout = connect_timeout
        self._transport: Optional[_DAPTransport] = None
        self._pending: Dict[int, asyncio.Future] = {}
        self._events: asyncio.Queue = asyncio.Queue()
        self._seq = 0
        self._reader_task: Optional[asyncio.Task] = None
        self._snapshot_seq = 0

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> "DAPClient":
        await self.connect()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.disconnect()

    # ------------------------------------------------------------------
    # Connection lifecycle
    # ------------------------------------------------------------------

    async def connect(self) -> None:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(self.host, self.port),
            timeout=self.connect_timeout,
        )
        self._transport = _DAPTransport(reader, writer)
        self._reader_task = asyncio.create_task(self._read_loop())
        logger.info("DAP connection established %s:%s", self.host, self.port)

    async def disconnect(self) -> None:
        if self._reader_task:
            self._reader_task.cancel()
        if self._transport:
            await self._transport.close()

    # ------------------------------------------------------------------
    # Background reader
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        while True:
            try:
                msg = await self._transport.recv()
            except (asyncio.IncompleteReadError, ConnectionResetError):
                break
            except Exception as exc:  # noqa: BLE001
                logger.debug("DAP read error: %s", exc)
                break

            if msg.get("type") == "response":
                req_seq = msg.get("request_seq")
                fut = self._pending.pop(req_seq, None)
                if fut and not fut.done():
                    fut.set_result(msg)
            elif msg.get("type") == "event":
                await self._events.put(msg)
            else:
                logger.debug("Unhandled DAP message: %s", msg.get("type"))

    # ------------------------------------------------------------------
    # Request helpers
    # ------------------------------------------------------------------

    async def _request(self, command: str, arguments: Optional[dict] = None) -> dict:
        self._seq += 1
        seq = self._seq
        msg = {"type": "request", "seq": seq, "command": command}
        if arguments:
            msg["arguments"] = arguments
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[seq] = fut
        await self._transport.send(msg)
        return await asyncio.wait_for(fut, timeout=10.0)

    # ------------------------------------------------------------------
    # DAP protocol methods
    # ------------------------------------------------------------------

    async def initialize(self) -> dict:
        return await self._request("initialize", {
            "clientID": "flowdelta",
            "clientName": "FlowDelta",
            "adapterID": "python",
            "locale": "en-US",
            "linesStartAt1": True,
            "columnsStartAt1": True,
            "pathFormat": "path",
            "supportsRunInTerminalRequest": False,
        })

    async def launch(
        self,
        program: str,
        args: Optional[List[str]] = None,
        cwd: Optional[str] = None,
        stop_on_entry: bool = False,
    ) -> dict:
        return await self._request("launch", {
            "program": program,
            "args": args or [],
            "cwd": cwd or "",
            "stopOnEntry": stop_on_entry,
            "noDebug": False,
        })

    async def attach(self, process_id: int) -> dict:
        return await self._request("attach", {"processId": process_id})

    async def set_breakpoints(self, source_path: str, lines: List[int]) -> dict:
        return await self._request("setBreakpoints", {
            "source": {"path": source_path},
            "breakpoints": [{"line": ln} for ln in lines],
        })

    async def configuration_done(self) -> dict:
        return await self._request("configurationDone")

    async def continue_execution(self, thread_id: int) -> dict:
        return await self._request("continue", {"threadId": thread_id})

    async def stack_trace(self, thread_id: int, levels: int = 10) -> List[StackFrame]:
        resp = await self._request("stackTrace", {
            "threadId": thread_id,
            "startFrame": 0,
            "levels": levels,
        })
        frames: List[StackFrame] = []
        for f in resp.get("body", {}).get("stackFrames", []):
            src = f.get("source", {})
            frames.append(StackFrame(
                id=f["id"],
                name=f.get("name", ""),
                source=src.get("path", src.get("name", "")),
                line=f.get("line", 0),
                column=f.get("column", 0),
            ))
        return frames

    async def scopes(self, frame_id: int) -> List[dict]:
        resp = await self._request("scopes", {"frameId": frame_id})
        return resp.get("body", {}).get("scopes", [])

    async def variables(self, variables_reference: int) -> Dict[str, Any]:
        resp = await self._request("variables", {"variablesReference": variables_reference})
        result: Dict[str, Any] = {}
        for var in resp.get("body", {}).get("variables", []):
            name = var.get("name", "")
            value = var.get("value", "")
            result[name] = value
        return result

    # ------------------------------------------------------------------
    # High-level: capture state at a breakpoint hit
    # ------------------------------------------------------------------

    async def capture_snapshot(self, stopped_event: dict) -> StateSnapshot:
        """Build a :class:`StateSnapshot` from a DAP ``stopped`` event."""
        thread_id = stopped_event.get("body", {}).get("threadId", 1)
        reason = stopped_event.get("body", {}).get("reason", "breakpoint")

        stack = await self.stack_trace(thread_id)
        top = stack[0] if stack else None

        locals_dict: Dict[str, Any] = {}
        if top:
            scopes = await self.scopes(top.id)
            for scope in scopes:
                if scope.get("name") in ("Locals", "locals"):
                    locals_dict = await self.variables(scope["variablesReference"])
                    break

        self._snapshot_seq += 1
        return StateSnapshot(
            event=reason,
            thread_id=thread_id,
            file=top.source if top else "",
            line=top.line if top else 0,
            function=top.name if top else "",
            stack=stack,
            locals=locals_dict,
            sequence=self._snapshot_seq,
        )

    # ------------------------------------------------------------------
    # High-level: iterate all breakpoint hits
    # ------------------------------------------------------------------

    async def iter_breakpoint_hits(self, timeout: float = 60.0) -> AsyncIterator[StateSnapshot]:
        """
        Yield a :class:`StateSnapshot` for each breakpoint hit until the
        program exits or *timeout* is reached.
        """
        while True:
            try:
                event = await asyncio.wait_for(self._events.get(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning("DAP event timeout after %ss", timeout)
                break

            event_name = event.get("event")

            if event_name == "stopped":
                snapshot = await self.capture_snapshot(event)
                yield snapshot
                thread_id = event.get("body", {}).get("threadId", 1)
                await self.continue_execution(thread_id)

            elif event_name in ("terminated", "exited"):
                logger.info("Target process finished (%s)", event_name)
                break
