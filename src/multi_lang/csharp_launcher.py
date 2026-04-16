"""C# / .NET DAP launcher for FlowDelta."""

from __future__ import annotations

import logging
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..state_tracker.dap_client import DAPClient, StateSnapshot
from ._base_launcher import BaseDAPLauncher
from .dap_normalize import normalize_dap_variables

logger = logging.getLogger(__name__)

# C#-specific DAP variable type sets
_CS_INT_TYPES = {"int", "long", "short", "byte", "Int32", "Int64"}
_CS_FLOAT_TYPES = {"double", "float", "decimal", "Double", "Single"}
_CS_BOOL_TYPES = {"bool", "Boolean"}
_CS_NULL_TYPES = {"null", "Null"}
_CS_STRING_TYPES = {"string", "String"}


class CSharpDAPLauncher(BaseDAPLauncher):
    """
    Launches a .NET application via ``netcoredbg`` and connects via DAP.

    Requires:
    - ``dotnet`` CLI on PATH
    - ``netcoredbg`` installed (https://github.com/Samsung/netcoredbg)
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
        """Convert a C# DAP ``variables`` response into FlowDelta's locals schema."""
        return normalize_dap_variables(
            raw,
            int_types=_CS_INT_TYPES,
            float_types=_CS_FLOAT_TYPES,
            bool_types=_CS_BOOL_TYPES,
            null_types=_CS_NULL_TYPES,
            string_types=_CS_STRING_TYPES,
        )
