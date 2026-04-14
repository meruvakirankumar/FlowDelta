"""
LSP Client – Phase 2 of FlowDelta.

Launches a Language Server (pylsp or pyright) via stdio and issues
LSP requests to enrich captured variable state with:
  - Inferred type information (textDocument/hover)
  - Symbol definitions (textDocument/definition)
  - Document symbol index (textDocument/documentSymbols)

This provides richer semantic context for delta analysis — e.g., knowing
that a variable changed from ``List[Order]`` to ``List[Order]`` with one
extra item is more informative than just seeing a raw list diff.

LSP reference: https://microsoft.github.io/language-server-protocol/
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import subprocess
import sys
from typing import Any, Dict, List, Optional
from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# LSP message helpers
# ---------------------------------------------------------------------------

def _make_request(method: str, params: dict, req_id: int) -> bytes:
    body = json.dumps({
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": params,
    }).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    return header + body


def _make_notification(method: str, params: dict) -> bytes:
    body = json.dumps({
        "jsonrpc": "2.0",
        "method": method,
        "params": params,
    }).encode("utf-8")
    header = f"Content-Length: {len(body)}\r\n\r\n".encode("utf-8")
    return header + body


# ---------------------------------------------------------------------------
# LSP stdio transport
# ---------------------------------------------------------------------------

class _LSPTransport:
    def __init__(self, proc: asyncio.subprocess.Process) -> None:
        self._proc = proc
        self._reader = proc.stdout
        self._writer = proc.stdin

    async def send(self, data: bytes) -> None:
        self._writer.write(data)
        await self._writer.drain()

    async def recv(self) -> Optional[dict]:
        try:
            header_raw = b""
            while True:
                line = await asyncio.wait_for(self._reader.readline(), timeout=5.0)
                if line in (b"\r\n", b"\n", b""):
                    break
                header_raw += line

            content_length = 0
            for part in header_raw.split(b"\r\n"):
                if part.lower().startswith(b"content-length:"):
                    content_length = int(part.split(b":", 1)[1].strip())

            if content_length == 0:
                return None
            body = await asyncio.wait_for(
                self._reader.readexactly(content_length), timeout=5.0
            )
            return json.loads(body.decode("utf-8"))
        except (asyncio.TimeoutError, asyncio.IncompleteReadError):
            return None

    async def close(self) -> None:
        try:
            self._writer.close()
            await asyncio.wait_for(self._proc.wait(), timeout=3.0)
        except Exception:
            self._proc.kill()


# ---------------------------------------------------------------------------
# LSP Client
# ---------------------------------------------------------------------------

class LSPClient:
    """
    Minimal LSP client that enriches variable state with type information.

    Example::

        async with LSPClient(root_path="/my/project") as lsp:
            await lsp.open_document("src/app.py")
            type_info = await lsp.hover("src/app.py", line=42, column=8)
            symbols = await lsp.document_symbols("src/app.py")
    """

    # Default server commands by name
    _SERVERS = {
        "pylsp": [sys.executable, "-m", "pylsp"],
        "pyright": ["pyright-langserver", "--stdio"],
        "pyright-python": [sys.executable, "-m", "pyright.langserver", "--stdio"],
    }

    def __init__(
        self,
        root_path: str,
        server: str = "pylsp",
    ) -> None:
        self.root_path = str(Path(root_path).resolve())
        self.server_name = server
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._transport: Optional[_LSPTransport] = None
        self._req_id = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._reader_task: Optional[asyncio.Task] = None
        self._initialized = False

    async def __aenter__(self) -> "LSPClient":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.stop()

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    async def start(self) -> None:
        cmd = self._SERVERS.get(self.server_name, self.server_name.split())
        self._proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._transport = _LSPTransport(self._proc)
        self._reader_task = asyncio.create_task(self._read_loop())
        await self._initialize()
        logger.info("LSP server '%s' started", self.server_name)

    async def stop(self) -> None:
        if self._initialized:
            await self._notify("shutdown", {})
            await self._notify("exit", {})
        if self._reader_task:
            self._reader_task.cancel()
        if self._transport:
            await self._transport.close()

    # ------------------------------------------------------------------
    # Background reader
    # ------------------------------------------------------------------

    async def _read_loop(self) -> None:
        while True:
            if not self._transport:
                break
            msg = await self._transport.recv()
            if msg is None:
                continue
            req_id = msg.get("id")
            if req_id is not None and req_id in self._pending:
                fut = self._pending.pop(req_id)
                if not fut.done():
                    fut.set_result(msg)

    # ------------------------------------------------------------------
    # Request primitives
    # ------------------------------------------------------------------

    async def _request(self, method: str, params: dict) -> dict:
        self._req_id += 1
        rid = self._req_id
        data = _make_request(method, params, rid)
        loop = asyncio.get_event_loop()
        fut: asyncio.Future = loop.create_future()
        self._pending[rid] = fut
        await self._transport.send(data)
        return await asyncio.wait_for(fut, timeout=10.0)

    async def _notify(self, method: str, params: dict) -> None:
        data = _make_notification(method, params)
        await self._transport.send(data)

    # ------------------------------------------------------------------
    # LSP initialization
    # ------------------------------------------------------------------

    async def _initialize(self) -> None:
        root_uri = Path(self.root_path).as_uri()
        await self._request("initialize", {
            "processId": os.getpid(),
            "rootUri": root_uri,
            "rootPath": self.root_path,
            "capabilities": {
                "textDocument": {
                    "hover": {"contentFormat": ["plaintext"]},
                    "documentSymbol": {"hierarchicalDocumentSymbolSupport": True},
                }
            },
            "initializationOptions": {},
        })
        await self._notify("initialized", {})
        self._initialized = True

    # ------------------------------------------------------------------
    # Public document operations
    # ------------------------------------------------------------------

    async def open_document(self, filepath: str) -> None:
        """Notify the LSP server about a file so it can index it."""
        path = Path(filepath)
        uri = path.as_uri()
        content = path.read_text(encoding="utf-8")
        await self._notify("textDocument/didOpen", {
            "textDocument": {
                "uri": uri,
                "languageId": self._language_id(path.suffix),
                "version": 1,
                "text": content,
            }
        })

    async def hover(self, filepath: str, line: int, column: int) -> Optional[str]:
        """
        Return hover (type / documentation) text at *line*, *column*.
        Lines and columns are 1-based; internally converted to 0-based for LSP.
        """
        uri = Path(filepath).as_uri()
        resp = await self._request("textDocument/hover", {
            "textDocument": {"uri": uri},
            "position": {"line": line - 1, "character": column - 1},
        })
        contents = resp.get("result", {})
        if contents is None:
            return None
        if isinstance(contents, dict):
            return contents.get("contents", {}).get("value") or str(contents.get("contents", ""))
        return str(contents)

    async def document_symbols(self, filepath: str) -> List[dict]:
        """Return all symbols (functions, classes, vars) in *filepath*."""
        uri = Path(filepath).as_uri()
        resp = await self._request("textDocument/documentSymbol", {
            "textDocument": {"uri": uri}
        })
        return resp.get("result") or []

    async def type_at(self, filepath: str, line: int, column: int) -> Optional[str]:
        """Convenience wrapper: return just the type string from hover."""
        text = await self.hover(filepath, line, column)
        if not text:
            return None
        # Parse common patterns: "name: TypeName" or "(variable) name: TypeName"
        import re
        m = re.search(r":\s*([A-Za-z_][A-Za-z0-9_\[\], |]*)", text)
        return m.group(1).strip() if m else text.split("\n")[0].strip()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _language_id(suffix: str) -> str:
        return {"py": "python", "js": "javascript", "ts": "typescript"}.get(
            suffix.lstrip(".").lower(), "plaintext"
        )
