"""Java DAP launcher for FlowDelta."""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..state_tracker.dap_client import DAPClient, StateSnapshot
from ._base_launcher import BaseDAPLauncher
from .dap_normalize import normalize_dap_variables

logger = logging.getLogger(__name__)

# Java-specific DAP variable type sets
_JAVA_INT_TYPES = {"int", "long", "short", "byte"}
_JAVA_FLOAT_TYPES = {"double", "float"}
_JAVA_BOOL_TYPES = {"boolean"}
_JAVA_NULL_TYPES = {"null"}
_JAVA_STRING_TYPES = {"String"}


class JavaDAPLauncher(BaseDAPLauncher):
    """
    Launches a Java application under ``java-debug`` and connects via DAP.

    Requires:
    - ``java`` on PATH
    - ``com.microsoft.java.debug.plugin`` JAR (Eclipse / VSCode Java extension)
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
        """Convert a Java DAP ``variables`` response into FlowDelta's locals schema."""
        return normalize_dap_variables(
            raw,
            int_types=_JAVA_INT_TYPES,
            float_types=_JAVA_FLOAT_TYPES,
            bool_types=_JAVA_BOOL_TYPES,
            null_types=_JAVA_NULL_TYPES,
            string_types=_JAVA_STRING_TYPES,
        )
