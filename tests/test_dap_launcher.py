"""
Tests for DAPLauncher.

Uses unittest.mock to avoid needing a real debugpy server.
"""

from __future__ import annotations

import asyncio
import socket
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import pytest
from src.state_tracker.dap_launcher import DAPLauncher, _DEFAULT_HOST, _DEFAULT_PORT


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def async_test(coro):
    """Decorator to run an async test function."""
    def wrapper(*args, **kwargs):
        asyncio.get_event_loop().run_until_complete(coro(*args, **kwargs))
    wrapper.__name__ = coro.__name__
    return wrapper


# ---------------------------------------------------------------------------
# Unit tests – no real subprocess
# ---------------------------------------------------------------------------

class TestDAPLauncherInit:
    def test_defaults(self):
        launcher = DAPLauncher("app.py")
        assert launcher.script == "app.py"
        assert launcher.host == _DEFAULT_HOST
        assert launcher.port == _DEFAULT_PORT
        assert launcher.script_args == []
        assert launcher.breakpoints == {}

    def test_custom_params(self):
        launcher = DAPLauncher(
            "app.py",
            script_args=["--debug"],
            host="0.0.0.0",
            port=6000,
            breakpoints={"app.py": [10, 20]},
        )
        assert launcher.port == 6000
        assert launcher.script_args == ["--debug"]
        assert launcher.breakpoints == {"app.py": [10, 20]}

    def test_attach_constructor(self):
        launcher = DAPLauncher.attach(pid=1234, port=5679)
        assert launcher._attach_pid == 1234
        assert launcher.script == ""


class TestDAPLauncherPortPoll:
    def test_port_open_returns_false_when_nothing_listening(self):
        launcher = DAPLauncher("app.py", port=19999)
        assert launcher._port_open() is False

    def test_port_open_returns_true_when_listening(self):
        """Spin up a real server socket briefly and verify detection."""
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as srv:
            srv.bind((_DEFAULT_HOST, 0))
            srv.listen(1)
            free_port = srv.getsockname()[1]
            launcher = DAPLauncher("app.py", port=free_port)
            assert launcher._port_open() is True


class TestDAPLauncherConnect:
    """Test _connect() by mocking DAPClient."""

    @pytest.mark.asyncio
    async def test_connect_calls_initialize(self):
        mock_client = AsyncMock()
        mock_client.connect = AsyncMock()
        mock_client.initialize = AsyncMock(return_value={})
        mock_client.set_breakpoints = AsyncMock(return_value={})
        mock_client.disconnect = AsyncMock()

        with patch("src.state_tracker.dap_launcher.DAPClient", return_value=mock_client):
            launcher = DAPLauncher("app.py", breakpoints={"app.py": [5]})
            launcher._proc = None
            result = await launcher._connect()

        mock_client.connect.assert_called_once()
        mock_client.initialize.assert_called_once()
        mock_client.set_breakpoints.assert_called_once_with("app.py", [5])
        assert result is mock_client

    @pytest.mark.asyncio
    async def test_wait_for_port_raises_on_timeout(self):
        launcher = DAPLauncher("app.py", port=19998, ready_timeout=0.1)
        with pytest.raises(TimeoutError, match="did not open"):
            await launcher._wait_for_port()

    @pytest.mark.asyncio
    async def test_aexit_terminates_proc(self):
        mock_proc = MagicMock()
        mock_proc.terminate = MagicMock()
        mock_proc.wait = MagicMock()

        mock_client = AsyncMock()
        mock_client.disconnect = AsyncMock()

        launcher = DAPLauncher("app.py")
        launcher._proc = mock_proc
        launcher._client = mock_client

        await launcher.__aexit__(None, None, None)

        mock_client.disconnect.assert_called_once()
        mock_proc.terminate.assert_called_once()


class TestDAPLauncherAttachMode:
    def test_attach_factory_sets_pid(self):
        launcher = DAPLauncher.attach(pid=9999, port=5700)
        assert launcher._attach_pid == 9999
        assert launcher.port == 5700
        assert launcher._proc is None

    @pytest.mark.asyncio
    async def test_attach_calls_attach_on_client(self):
        mock_client = AsyncMock()
        mock_client.connect = AsyncMock()
        mock_client.initialize = AsyncMock(return_value={})
        mock_client.attach = AsyncMock(return_value={})
        mock_client.disconnect = AsyncMock()

        with patch("src.state_tracker.dap_launcher.DAPClient", return_value=mock_client):
            launcher = DAPLauncher.attach(pid=42, port=5700)

            # Patch _wait_for_port to skip polling
            async def _noop():
                pass
            launcher._wait_for_port = _noop

            result = await launcher._connect_attach()

        mock_client.attach.assert_called_once_with(42)
        assert result is mock_client
