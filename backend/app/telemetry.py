"""Optional OpenTelemetry tracing. Disabled unless OTEL_EXPORTER_OTLP_ENDPOINT is set.

No prompts, results or customer data are put on spans - only identifiers, kinds and timings.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from app.config import get_settings

_tracer: Any = None


def setup_tracing(service_name: str) -> bool:
    global _tracer
    endpoint = get_settings().otel_exporter_otlp_endpoint
    if not endpoint:
        return False
    from opentelemetry import trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    provider = TracerProvider(resource=Resource.create({"service.name": service_name}))
    provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint.rstrip("/") + "/v1/traces")))
    trace.set_tracer_provider(provider)
    _tracer = trace.get_tracer("metric-investigator")
    return True


@contextmanager
def span(name: str, attributes: dict[str, Any] | None = None) -> Iterator[None]:
    if _tracer is None:
        yield
        return
    with _tracer.start_as_current_span(name, attributes=attributes or {}):
        yield
