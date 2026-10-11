"""Optional OpenTelemetry tracing (pip install commons-memory[otel]).

COMMONS_OTEL=1 enables spans for MCP tool calls, LLM calls, MCP-hub upstream calls, orchestration claims/completions,
search and background jobs. Export: OTLP when `opentelemetry-exporter-otlp` is installed and OTEL_EXPORTER_OTLP_ENDPOINT
is set; COMMONS_OTEL=console prints spans; otherwise spans stay in-process (useful for tests via `memory_spans()`).
Span attributes never include argument values or secrets — only names, ids, sizes and timings.
"""
from __future__ import annotations

import contextlib
import os

_tracer = None
_memory = None


def enabled() -> bool:
    return os.environ.get("COMMONS_OTEL", "0") not in ("0", "", "off")


def tracer():
    global _tracer, _memory
    if _tracer is not None or not enabled():
        return _tracer
    try:
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import SimpleSpanProcessor
    except ImportError:
        return None
    from . import __version__
    provider = TracerProvider(resource=Resource.create({"service.name": "commons", "service.version": __version__}))
    mode = os.environ.get("COMMONS_OTEL")
    if mode == "console":
        from opentelemetry.sdk.trace.export import ConsoleSpanExporter
        provider.add_span_processor(SimpleSpanProcessor(ConsoleSpanExporter()))
    elif os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT"):
        try:
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        except ImportError:
            pass
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    _memory = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(_memory))
    _tracer = provider.get_tracer("commons")
    return _tracer


@contextlib.contextmanager
def span(name: str, **attrs):
    t = tracer()
    if t is None:
        yield None
        return
    with t.start_as_current_span(name) as sp:
        for k, v in attrs.items():
            if v is not None:
                sp.set_attribute(f"commons.{k}", v if isinstance(v, (int, float, bool)) else str(v)[:200])
        yield sp


def memory_spans() -> list:
    return list(_memory.get_finished_spans()) if _memory else []


def instrument_mcp(mcp) -> None:
    """Wrap every MCP tool call in a span (tool name, duration, error flag)."""
    if not enabled():
        return
    tm = mcp._tool_manager
    orig = tm.call_tool

    async def traced(name, arguments, *a, **kw):
        with span(f"mcp.tool {name}", tool=name, args=len(arguments or {})) as sp:
            try:
                return await orig(name, arguments, *a, **kw)
            except Exception:
                if sp is not None:
                    sp.set_attribute("commons.error", True)
                raise
    tm.call_tool = traced
