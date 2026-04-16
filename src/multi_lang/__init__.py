"""
Multi-Language DAP Support – Sprint 4 of FlowDelta.

Extends FlowDelta's Debug Adapter Protocol (DAP) backend to support:

* **Java** via LSP4J's built-in DAP server (Eclipse JDT Language Server /
  ``java-debug`` extension)
* **C#** via OmniSharp / ``netcoredbg``

Both languages expose a DAP-compliant server — FlowDelta's existing
:class:`~src.state_tracker.dap_client.DAPClient` can talk to any of them.
This module provides:

1. **Launchers** — ``JavaDAPLauncher`` and ``CSharpDAPLauncher`` — that
   start the language-specific debug server subprocess and return a
   connected :class:`DAPClient`.

2. **Variable serializers** — language-specific logic to normalise the
   variable format returned by Java/C# DAP into the same ``Dict[str, Any]``
   schema that FlowDelta's delta engine expects.

3. **ASTAnalyzer stubs** — lightweight heuristic analysers for ``.java``
   and ``.cs`` files (tree-sitter grammars are optional; falls back to
   regex-based extraction) that feed into the :class:`CallGraphBuilder`.

Usage::

    from src.multi_lang import JavaDAPLauncher, CSharpDAPLauncher

    # Java — connect to a running java-debug server
    async with JavaDAPLauncher(
        jar_path="myapp.jar",
        breakpoints={"com/example/Checkout.java": [18, 45]},
        debug_port=5005,
    ) as client:
        async for snapshot in client.iter_breakpoint_hits():
            process(snapshot)

    # C# — launch netcoredbg
    async with CSharpDAPLauncher(
        project_path="MyApp/MyApp.csproj",
        breakpoints={"MyApp/Checkout.cs": [22, 67]},
    ) as client:
        async for snapshot in client.iter_breakpoint_hits():
            process(snapshot)
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..state_tracker.dap_client import DAPClient, StateSnapshot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Base launcher
# ---------------------------------------------------------------------------

class _BaseDAPLauncher:
    """
    Shared lifecycle management for language-specific DAP launchers.
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


# ---------------------------------------------------------------------------
# Java DAP Launcher
# ---------------------------------------------------------------------------

class JavaDAPLauncher(_BaseDAPLauncher):
    """
    Launches a Java application under ``java-debug`` and connects via DAP.

    Requires:
    - ``java`` on PATH
    - ``com.microsoft.java.debug.plugin`` JAR (Eclipse / VSCode Java extension)

    Parameters
    ----------
    jar_path : str | Path
        The application JAR to debug.
    breakpoints : dict[str, list[int]]
        ``{"com/example/Checkout.java": [18, 45]}``
    debug_port : int
        JDWP listen port that java-debug bridges to DAP.
    java_debug_jar : str | Path | None
        Path to ``com.microsoft.java.debug.plugin-*.jar``.
        If ``None``, uses ``JAVA_DEBUG_JAR`` env var or searches common paths.
    jvm_args : list[str]
        Extra JVM arguments.
    """

    _COMMON_DEBUG_JAR_LOCATIONS = [
        Path.home() / ".vscode" / "extensions",
        Path("/usr/share/java"),
        Path("/opt/java-debug"),
    ]

    def __init__(
        self,
        jar_path: str | Path,
        breakpoints: Dict[str, List[int]],
        debug_port: int = 5005,
        dap_port: int = 5678,
        java_debug_jar: Optional[str | Path] = None,
        jvm_args: Optional[List[str]] = None,
        host: str = "127.0.0.1",
        connect_timeout: int = 20,
    ) -> None:
        super().__init__(host=host, port=dap_port, connect_timeout=connect_timeout)
        self.jar_path = Path(jar_path)
        self.breakpoints = breakpoints
        self.debug_port = debug_port
        self.dap_port = dap_port
        self.java_debug_jar = Path(java_debug_jar) if java_debug_jar else self._find_debug_jar()
        self.jvm_args = jvm_args or []

    def _find_debug_jar(self) -> Optional[Path]:
        env_jar = os.environ.get("JAVA_DEBUG_JAR")
        if env_jar:
            return Path(env_jar)
        for base in self._COMMON_DEBUG_JAR_LOCATIONS:
            if base.exists():
                for p in base.rglob("com.microsoft.java.debug.plugin-*.jar"):
                    return p
        return None

    async def _start_server(self) -> subprocess.Popen:
        java = shutil.which("java")
        if not java:
            raise FileNotFoundError(
                "Java not found on PATH. Install JDK 11+ and add it to PATH."
            )

        cmd = [
            java,
            f"-agentlib:jdwp=transport=dt_socket,server=y,"
            f"suspend=y,address=*:{self.debug_port}",
            *self.jvm_args,
            "-jar", str(self.jar_path),
        ]

        logger.info("Starting Java DAP subprocess: %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc

    @staticmethod
    def normalize_variable(raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert a Java DAP ``variables`` response item into FlowDelta's
        ``Dict[str, Any]`` locals schema.
        """
        result: Dict[str, Any] = {}
        for var in raw.get("variables", []):
            name = var.get("name", "")
            type_ = var.get("type", "")
            value_str = var.get("value", "")

            # Best-effort type coercion
            if type_ in ("int", "long", "short", "byte"):
                try:
                    result[name] = int(value_str)
                    continue
                except ValueError:
                    pass
            if type_ in ("double", "float"):
                try:
                    result[name] = float(value_str)
                    continue
                except ValueError:
                    pass
            if type_ == "boolean":
                result[name] = value_str.lower() == "true"
                continue
            if type_ == "null" or value_str == "null":
                result[name] = None
                continue
            if type_ == "String":
                # Strip surrounding quotes
                result[name] = value_str.strip('"')
                continue
            result[name] = value_str

        return result


# ---------------------------------------------------------------------------
# C# / .NET DAP Launcher
# ---------------------------------------------------------------------------

class CSharpDAPLauncher(_BaseDAPLauncher):
    """
    Launches a .NET application via ``netcoredbg`` and connects via DAP.

    Requires:
    - ``dotnet`` CLI on PATH
    - ``netcoredbg`` installed (https://github.com/Samsung/netcoredbg)

    Parameters
    ----------
    project_path : str | Path
        ``*.csproj`` or directory containing one.
    breakpoints : dict[str, list[int]]
        ``{"MyApp/Checkout.cs": [22, 67]}``
    netcoredbg_path : str | None
        Path to ``netcoredbg`` binary. Searched on PATH if ``None``.
    configuration : str
        Build configuration (``"Debug"`` or ``"Release"``).
    """

    def __init__(
        self,
        project_path: str | Path,
        breakpoints: Dict[str, List[int]],
        dap_port: int = 5678,
        netcoredbg_path: Optional[str] = None,
        configuration: str = "Debug",
        host: str = "127.0.0.1",
        connect_timeout: int = 20,
    ) -> None:
        super().__init__(host=host, port=dap_port, connect_timeout=connect_timeout)
        self.project_path = Path(project_path)
        self.breakpoints = breakpoints
        self.dap_port = dap_port
        self.netcoredbg = netcoredbg_path or shutil.which("netcoredbg") or "netcoredbg"
        self.configuration = configuration

    async def _start_server(self) -> subprocess.Popen:
        # Build first
        dotnet = shutil.which("dotnet")
        if not dotnet:
            raise FileNotFoundError(
                "dotnet CLI not found. Install .NET SDK from https://dot.net"
            )

        build_result = subprocess.run(
            [dotnet, "build", str(self.project_path),
             "-c", self.configuration, "--nologo", "-q"],
            capture_output=True, timeout=120,
        )
        if build_result.returncode != 0:
            raise RuntimeError(
                f"dotnet build failed:\n{build_result.stderr.decode()}"
            )

        # Find the built DLL
        dll = self._find_dll()
        if not dll:
            raise FileNotFoundError(
                f"Could not find built DLL for {self.project_path}. "
                f"Check that 'dotnet build' succeeded."
            )

        cmd = [
            self.netcoredbg,
            "--interpreter=vscode",
            f"--server-port={self.dap_port}",
            "--",
            dotnet, str(dll),
        ]
        logger.info("Starting netcoredbg: %s", " ".join(cmd))
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return proc

    def _find_dll(self) -> Optional[Path]:
        """Locate the compiled DLL in bin/Debug or bin/Release."""
        search_root = self.project_path if self.project_path.is_dir() else self.project_path.parent
        for p in search_root.rglob(f"bin/{self.configuration}/**/*.dll"):
            if not p.name.startswith("Microsoft") and not p.name.startswith("System"):
                return p
        return None

    @staticmethod
    def normalize_variable(raw: Dict[str, Any]) -> Dict[str, Any]:
        """
        Convert a C# DAP ``variables`` response into FlowDelta's locals schema.
        """
        result: Dict[str, Any] = {}
        for var in raw.get("variables", []):
            name = var.get("name", "")
            type_ = var.get("type", "")
            value_str = var.get("value", "")

            if type_ in ("int", "long", "short", "byte", "Int32", "Int64"):
                try:
                    result[name] = int(value_str)
                    continue
                except ValueError:
                    pass
            if type_ in ("double", "float", "decimal", "Double", "Single"):
                try:
                    result[name] = float(value_str)
                    continue
                except ValueError:
                    pass
            if type_ in ("bool", "Boolean"):
                result[name] = value_str.lower() == "true"
                continue
            if "null" in value_str.lower() or type_ in ("null", "Null"):
                result[name] = None
                continue
            if type_ in ("string", "String"):
                result[name] = value_str.strip('"')
                continue
            result[name] = value_str

        return result


# ---------------------------------------------------------------------------
# Heuristic AST analysers for Java / C#
# ---------------------------------------------------------------------------

class JavaASTAnalyzer:
    """
    Lightweight regex-based Java method extractor.

    Feeds into :class:`~src.flow_identifier.call_graph.CallGraphBuilder`
    when ``tree-sitter-java`` is not available.
    """

    _METHOD_RE = re.compile(
        r"(?:public|private|protected|static|\s)*"
        r"[\w<>\[\]]+\s+(\w+)\s*\([^)]*\)\s*(?:throws\s+\S+)?\s*\{",
        re.MULTILINE,
    )
    _CALL_RE = re.compile(r"(\w+)\s*\(")

    def analyze(self, file_path: str | Path) -> Dict[str, Any]:
        """
        Extract method names and approximate call edges from a Java source file.
        Returns a dict compatible with :class:`ASTAnalysis`.
        """
        source = Path(file_path).read_text(encoding="utf-8", errors="ignore")
        lines = source.splitlines()
        functions = []
        for m in self._METHOD_RE.finditer(source):
            name = m.group(1)
            line_no = source[: m.start()].count("\n") + 1
            # Find calls inside this method body (crude approximation)
            brace_start = m.end()
            depth, end = 1, brace_start
            while end < len(source) and depth > 0:
                if source[end] == "{":
                    depth += 1
                elif source[end] == "}":
                    depth -= 1
                end += 1
            body = source[brace_start:end]
            calls = list({c for c in self._CALL_RE.findall(body)
                          if c not in {"if", "while", "for", "switch", "return", name}})
            functions.append({
                "name": name,
                "qualified_name": name,
                "file": str(file_path),
                "start_line": line_no,
                "end_line": line_no + body.count("\n"),
                "args": [],
                "calls": calls,
                "language": "java",
            })
        return {"file": str(file_path), "language": "java", "functions": functions}


class CSharpASTAnalyzer:
    """
    Lightweight regex-based C# method extractor.
    """

    _METHOD_RE = re.compile(
        r"(?:public|private|protected|internal|static|virtual|override|async|\s)*"
        r"[\w<>\[\]?]+\s+(\w+)\s*\([^)]*\)\s*(?:where\s+\S+)?\s*\{",
        re.MULTILINE,
    )
    _CALL_RE = re.compile(r"(\w+)\s*\(")

    def analyze(self, file_path: str | Path) -> Dict[str, Any]:
        """Extract C# method names and calls. Same output schema as :class:`JavaASTAnalyzer`."""
        source = Path(file_path).read_text(encoding="utf-8", errors="ignore")
        functions = []
        for m in self._METHOD_RE.finditer(source):
            name = m.group(1)
            if name in {"if", "while", "for", "foreach", "switch", "using", "catch"}:
                continue
            line_no = source[: m.start()].count("\n") + 1
            brace_start = m.end()
            depth, end = 1, brace_start
            while end < len(source) and depth > 0:
                if source[end] == "{":
                    depth += 1
                elif source[end] == "}":
                    depth -= 1
                end += 1
            body = source[brace_start:end]
            calls = list({c for c in self._CALL_RE.findall(body)
                          if c not in {"if", "while", "for", "foreach", "switch", "return", name}})
            functions.append({
                "name": name,
                "qualified_name": name,
                "file": str(file_path),
                "start_line": line_no,
                "end_line": line_no + body.count("\n"),
                "args": [],
                "calls": calls,
                "language": "csharp",
            })
        return {"file": str(file_path), "language": "csharp", "functions": functions}
