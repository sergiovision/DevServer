"""Governance-grade OpenTelemetry tracing — opt-in, zero overhead when off.

DevServer exports its task pipeline as OTLP spans whose attributes carry the
governance signal neither competitor exposes: reality score, budget burn,
secret-scan hits, error class, and vendor failover. Devin exports no telemetry
at all (closed SaaS); CrewAI emits only generic GenAI traces.

Activation is entirely opt-in: tracing turns on **only** when
``OTEL_EXPORTER_OTLP_ENDPOINT`` is set *and* the opentelemetry packages import
cleanly. Otherwise every public function here is a cheap no-op, so deployments
that don't want OTEL pay nothing.

The whole module is one wrapper around the single ``_emit_event`` chokepoint in
``agent_runner``: a per-task root span is opened at task start, every
``task_events`` emission becomes a span event (plus a durable governance
attribute on the root span), and the span closes at the terminal status. No
span objects are threaded through the call sites — they're keyed by ``task_id``
in a module-level map.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

_enabled = False
_tracer = None
# task_id → active root span (only populated when tracing is enabled)
_spans: dict[int, object] = {}


def init_telemetry() -> None:
    """Configure the OTLP exporter once at startup. No-op unless the endpoint
    env var is set and the opentelemetry packages are importable."""
    global _enabled, _tracer

    if not os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"):
        return
    try:
        from opentelemetry import trace
        from opentelemetry.sdk.resources import Resource
        from opentelemetry.sdk.trace import TracerProvider
        from opentelemetry.sdk.trace.export import BatchSpanProcessor
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import (
            OTLPSpanExporter,
        )
    except Exception:
        logger.warning(
            "OTEL_EXPORTER_OTLP_ENDPOINT is set but the opentelemetry packages "
            "are not installed — tracing disabled. Install the worker's otel "
            "extras to enable it."
        )
        return

    try:
        resource = Resource.create(
            {"service.name": os.getenv("OTEL_SERVICE_NAME", "devserver-worker")}
        )
        provider = TracerProvider(resource=resource)
        # OTLPSpanExporter reads the endpoint from the standard OTEL_* env vars.
        provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(provider)
        _tracer = trace.get_tracer("devserver")
        _enabled = True
        logger.info(
            "OpenTelemetry tracing enabled → %s",
            os.getenv("OTEL_EXPORTER_OTLP_ENDPOINT"),
        )
    except Exception:
        logger.exception("Failed to initialise OpenTelemetry — tracing disabled")


def is_enabled() -> bool:
    return _enabled


def start_task_span(
    task_id: int,
    task_key: str,
    *,
    vendor: str | None = None,
    model: str | None = None,
    mode: str | None = None,
    repo_name: str | None = None,
) -> None:
    """Open the per-task root span. No-op when tracing is off."""
    if not _enabled or _tracer is None:
        return
    try:
        span = _tracer.start_span(f"task {task_key}")
        span.set_attribute("devserver.task_key", task_key)
        span.set_attribute("devserver.task_id", task_id)
        if vendor:
            span.set_attribute("gen_ai.system", vendor)
        if model:
            span.set_attribute("gen_ai.request.model", model)
        if mode:
            span.set_attribute("devserver.mode", mode)
        if repo_name:
            span.set_attribute("devserver.repo", repo_name)
        _spans[task_id] = span
    except Exception:
        logger.exception("telemetry.start_task_span failed")


def _set(span, key: str, value) -> None:
    """Set a span attribute only when the value is a usable primitive."""
    if value is None:
        return
    if isinstance(value, (str, bool, int, float)):
        span.set_attribute(key, value)


def _map_governance(span, event_type: str, payload: dict) -> None:
    """Map a task event onto durable governance attributes on the root span.
    These are the signals neither Devin nor CrewAI expose."""
    if event_type in ("reality_signal", "reality_abstain"):
        _set(span, "devserver.reality_score", payload.get("score"))
        if event_type == "reality_abstain":
            _set(span, "devserver.reality_abstain", True)
            _set(span, "devserver.abstain_reason", payload.get("reason"))
    elif event_type in ("budget_warning", "budget_exceeded"):
        _set(span, "devserver.budget.cost_usd", payload.get("cum_cost_usd"))
        _set(span, "devserver.budget.wall_seconds", payload.get("cum_wall_seconds"))
        _set(span, "devserver.budget.reason", payload.get("reason"))
        if event_type == "budget_exceeded":
            _set(span, "devserver.budget.exceeded", True)
    elif event_type == "error_classified":
        _set(span, "devserver.error_class", payload.get("class"))
        _set(span, "devserver.error_severity", payload.get("severity"))
    elif event_type == "cost_update":
        _set(span, "gen_ai.usage.cost_usd", payload.get("cost_usd"))
        _set(span, "gen_ai.usage.turns", payload.get("turns"))
    elif event_type in ("pr_preflight_pass", "pr_preflight_fail"):
        # Pro preflight summary key names vary; surface the common ones.
        _set(span, "devserver.preflight.secret_hits",
             payload.get("secret_hits") or payload.get("secrets"))
        _set(span, "devserver.preflight.files_changed", payload.get("files_changed"))
        _set(span, "devserver.preflight.passed", event_type == "pr_preflight_pass")
    elif event_type == "vendor_failover":
        _set(span, "devserver.vendor_failover", True)
        _set(span, "devserver.failover.to_vendor", payload.get("to_vendor"))
        _set(span, "devserver.failover.to_model", payload.get("to_model"))
    elif event_type in ("plan_pending", "plan_approved", "plan_rejected"):
        _set(span, "devserver.plan_gate.state", event_type.replace("plan_", ""))


def record_event(task_id: int, event_type: str, payload: dict | None) -> None:
    """Attach a span event for a task_events emission and map its governance
    attributes onto the root span. No-op when tracing is off or no span is
    open for the task. Never raises."""
    if not _enabled:
        return
    span = _spans.get(task_id)
    if span is None:
        return
    try:
        payload = payload or {}
        attrs: dict[str, object] = {"devserver.event": event_type}
        for k, v in payload.items():
            if isinstance(v, (str, bool, int, float)):
                attrs[f"payload.{k}"] = v
        span.add_event(event_type, attributes=attrs)
        _map_governance(span, event_type, payload)
    except Exception:
        logger.exception("telemetry.record_event failed (%s)", event_type)


def end_task_span(task_id: int, *, status: str | None = None, success: bool | None = None) -> None:
    """Close the per-task root span. No-op when tracing is off."""
    if not _enabled:
        return
    span = _spans.pop(task_id, None)
    if span is None:
        return
    try:
        if status:
            _set(span, "devserver.final_status", status)
        if success is not None:
            _set(span, "devserver.success", success)
        span.end()
    except Exception:
        logger.exception("telemetry.end_task_span failed")
