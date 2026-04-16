"""
OpenTelemetry Trace Exporter – Sprint 4 of FlowDelta.

Converts :class:`FlowTrace` and :class:`TraceDelta` objects into
OpenTelemetry spans and exports them to any OTLP-compatible backend
(Jaeger, Grafana Tempo, Honeycomb, etc.).

Architecture
------------
Each FlowDelta trace maps to one OTel **trace** (same trace_id).
Each state snapshot becomes a **span**:

  FlowTrace  →  OTel Trace
    ├── Snapshot[0]  →  Span  (root span = flow entry)
    ├── Snapshot[1]  →  Span  (child of root)
    │     attributes: changed variables as span attributes
    ├── Snapshot[2]  →  Span
    └── ...

Each :class:`VariableDelta` is attached as a span **event** with attributes:
  - ``flowdelta.variable``  — variable name
  - ``flowdelta.change_type`` — added / removed / changed / type_changed
  - ``flowdelta.old_value`` / ``flowdelta.new_value``

Usage (requires ``opentelemetry-sdk`` + ``opentelemetry-exporter-otlp-proto-grpc``)::

    exporter = OTelExporter(endpoint="http://localhost:4317")
    exporter.export_trace(trace, delta)

Fallback: when OTel SDK is not installed, the exporter writes a
JSON-Lines file that matches the OTel log format so the data is
never silently dropped.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..state_tracker.trace_recorder import FlowTrace
from ..state_tracker.dap_client import StateSnapshot
from ..delta_engine.state_diff import TraceDelta, SnapshotDelta, VariableDelta

logger = logging.getLogger(__name__)

# OTel status codes
_STATUS_OK = "STATUS_CODE_OK"
_STATUS_ERROR = "STATUS_CODE_ERROR"


# ---------------------------------------------------------------------------
# Data classes (OTel-shaped, framework-independent)
# ---------------------------------------------------------------------------

@dataclass
class OTelAttribute:
    key: str
    value: Any

    def to_dict(self) -> dict:
        return {"key": self.key, "value": {"stringValue": str(self.value)}}


@dataclass
class OTelEvent:
    name: str
    time_unix_nano: int
    attributes: List[OTelAttribute] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "timeUnixNano": str(self.time_unix_nano),
            "attributes": [a.to_dict() for a in self.attributes],
        }


@dataclass
class OTelSpan:
    """One OTel span representing a single state snapshot."""
    trace_id: str
    span_id: str
    parent_span_id: Optional[str]
    name: str
    start_time_unix_nano: int
    end_time_unix_nano: int
    attributes: List[OTelAttribute] = field(default_factory=list)
    events: List[OTelEvent] = field(default_factory=list)
    status_code: str = _STATUS_OK

    def to_dict(self) -> dict:
        d = {
            "traceId": self.trace_id,
            "spanId": self.span_id,
            "name": self.name,
            "startTimeUnixNano": str(self.start_time_unix_nano),
            "endTimeUnixNano": str(self.end_time_unix_nano),
            "attributes": [a.to_dict() for a in self.attributes],
            "events": [e.to_dict() for e in self.events],
            "status": {"code": self.status_code},
        }
        if self.parent_span_id:
            d["parentSpanId"] = self.parent_span_id
        return d


@dataclass
class OTelResourceSpans:
    """OTLP ResourceSpans envelope."""
    flow_id: str
    run_id: str
    spans: List[OTelSpan] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "resourceSpans": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "flowdelta"}},
                        {"key": "flowdelta.flow_id", "value": {"stringValue": self.flow_id}},
                        {"key": "flowdelta.run_id", "value": {"stringValue": self.run_id}},
                    ]
                },
                "scopeSpans": [{
                    "scope": {"name": "flowdelta", "version": "0.1.0"},
                    "spans": [s.to_dict() for s in self.spans],
                }],
            }]
        }


# ---------------------------------------------------------------------------
# Exporter
# ---------------------------------------------------------------------------

class OTelExporter:
    """
    Exports FlowDelta traces as OpenTelemetry spans.

    Parameters
    ----------
    endpoint : str | None
        OTLP gRPC endpoint (e.g. ``"http://localhost:4317"``).
        If ``None``, falls back to file export.
    fallback_path : str | Path
        Where to write JSONL if SDK unavailable or endpoint unreachable.
    insecure : bool
        Skip TLS verification (useful for local Jaeger/Tempo).
    """

    def __init__(
        self,
        endpoint: Optional[str] = None,
        fallback_path: str | Path = ".flowdelta/otel",
        insecure: bool = True,
    ) -> None:
        self.endpoint = endpoint
        self.fallback_path = Path(fallback_path)
        self.insecure = insecure
        self.fallback_path.mkdir(parents=True, exist_ok=True)
        self._sdk_available = self._check_sdk()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def export_trace(
        self,
        trace: FlowTrace,
        delta: Optional[TraceDelta] = None,
    ) -> OTelResourceSpans:
        """
        Convert *trace* (and optional *delta*) to OTel spans and export them.

        Returns the :class:`OTelResourceSpans` for inspection / testing.
        """
        resource_spans = self._build_resource_spans(trace, delta)

        if self.endpoint and self._sdk_available:
            self._export_via_sdk(resource_spans)
        else:
            self._export_to_file(resource_spans)

        return resource_spans

    def export_to_json(self, resource_spans: OTelResourceSpans) -> str:
        """Return the OTLP JSON payload as a string."""
        return json.dumps(resource_spans.to_dict(), indent=2)

    # ------------------------------------------------------------------
    # Builder
    # ------------------------------------------------------------------

    def _build_resource_spans(
        self,
        trace: FlowTrace,
        delta: Optional[TraceDelta],
    ) -> OTelResourceSpans:
        trace_id = _make_trace_id(trace.run_id)
        # Build lookup: seq → SnapshotDelta
        delta_map: Dict[int, SnapshotDelta] = {}
        if delta:
            for sd in delta.deltas:
                delta_map[sd.to_seq] = sd

        resource_spans = OTelResourceSpans(
            flow_id=trace.flow_id,
            run_id=trace.run_id,
        )

        # Time base: use wall clock spread evenly across snapshots
        base_ns = int(datetime.now(timezone.utc).timestamp() * 1e9)
        step_ns = 1_000_000  # 1 ms per snapshot

        root_span_id: Optional[str] = None
        for i, snap in enumerate(trace.snapshots):
            span_id = _make_span_id(f"{trace.run_id}:{snap.sequence}")
            start_ns = base_ns + i * step_ns
            end_ns = start_ns + step_ns

            attrs = [
                OTelAttribute("flowdelta.file", snap.file.split("\\")[-1].split("/")[-1]),
                OTelAttribute("flowdelta.line", snap.line),
                OTelAttribute("flowdelta.function", snap.function),
                OTelAttribute("flowdelta.event", snap.event),
                OTelAttribute("flowdelta.sequence", snap.sequence),
            ]

            # Variable delta events for this snapshot
            events: List[OTelEvent] = []
            if snap.sequence in delta_map:
                for change in delta_map[snap.sequence].changes:
                    events.append(_change_to_event(change, start_ns + 100))

            span = OTelSpan(
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=root_span_id if i > 0 else None,
                name=f"{snap.function} [{snap.event}]",
                start_time_unix_nano=start_ns,
                end_time_unix_nano=end_ns,
                attributes=attrs,
                events=events,
                status_code=_STATUS_ERROR if snap.event == "exception" else _STATUS_OK,
            )
            resource_spans.spans.append(span)

            if i == 0:
                root_span_id = span_id

        return resource_spans

    # ------------------------------------------------------------------
    # Export backends
    # ------------------------------------------------------------------

    def _export_via_sdk(self, resource_spans: OTelResourceSpans) -> None:
        """Send spans via ``opentelemetry-exporter-otlp-proto-grpc``."""
        try:
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
            from opentelemetry import trace as otel_trace
            from opentelemetry.sdk.resources import Resource

            resource = Resource.create({
                "service.name": "flowdelta",
                "flowdelta.flow_id": resource_spans.flow_id,
                "flowdelta.run_id": resource_spans.run_id,
            })
            provider = TracerProvider(resource=resource)
            exporter = OTLPSpanExporter(
                endpoint=self.endpoint,
                insecure=self.insecure,
            )
            provider.add_span_processor(BatchSpanProcessor(exporter))
            otel_trace.set_tracer_provider(provider)

            tracer = otel_trace.get_tracer("flowdelta", "0.1.0")
            with tracer.start_as_current_span(
                f"flow:{resource_spans.flow_id}"
            ) as root:
                for span_data in resource_spans.spans:
                    with tracer.start_as_current_span(span_data.name) as s:
                        for attr in span_data.attributes:
                            s.set_attribute(attr.key, str(attr.value))
                        for event in span_data.events:
                            s.add_event(
                                event.name,
                                {a.key: str(a.value) for a in event.attributes},
                            )

            provider.force_flush()
            logger.info(
                "Exported %d spans to %s", len(resource_spans.spans), self.endpoint
            )
        except Exception as exc:
            logger.warning("OTel SDK export failed (%s); falling back to file", exc)
            self._export_to_file(resource_spans)

    def _export_to_file(self, resource_spans: OTelResourceSpans) -> None:
        """Write OTLP-JSON to a JSONL fallback file."""
        path = self.fallback_path / f"spans_{resource_spans.run_id}.jsonl"
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(resource_spans.to_dict()) + "\n")
        logger.info("OTel spans written to %s", path)

    def _check_sdk(self) -> bool:
        try:
            import opentelemetry  # noqa: F401
            return True
        except ImportError:
            return False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_trace_id(run_id: str) -> str:
    """Create a 32-hex-char trace ID from run_id."""
    h = uuid.uuid5(uuid.NAMESPACE_DNS, f"flowdelta:{run_id}").hex
    return h.ljust(32, "0")[:32]


def _make_span_id(key: str) -> str:
    """Create a 16-hex-char span ID."""
    return uuid.uuid5(uuid.NAMESPACE_DNS, key).hex[:16]


def _change_to_event(change: VariableDelta, time_ns: int) -> OTelEvent:
    """Convert a :class:`VariableDelta` to an OTel span event."""
    attrs = [
        OTelAttribute("flowdelta.variable", change.name),
        OTelAttribute("flowdelta.change_type", change.change_type),
    ]
    if change.old_value is not None:
        attrs.append(OTelAttribute("flowdelta.old_value", str(change.old_value)[:200]))
    if change.new_value is not None:
        attrs.append(OTelAttribute("flowdelta.new_value", str(change.new_value)[:200]))
    return OTelEvent(
        name=f"variable.{change.change_type}",
        time_unix_nano=time_ns,
        attributes=attrs,
    )
