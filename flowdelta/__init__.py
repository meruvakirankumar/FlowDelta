"""
flowdelta package shim.

Re-exports the public SDK from the ``src`` package so that both import styles work::

    from flowdelta import FlowDelta, observe, track   # external users
    from src import FlowDelta, observe, track          # internal / editable install
"""

from src.sdk import FlowDelta, observe, track
from src.orchestrator import FlowDeltaPipeline, cli

__all__ = ["FlowDelta", "observe", "track", "FlowDeltaPipeline", "cli"]
