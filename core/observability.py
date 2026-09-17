"""
Observability — LangSmith tracing setup, OpenTelemetry spans, and business metrics.

Call configure_langsmith() then configure_tracing() once at app startup
before any LangChain/LangGraph imports.
"""
from __future__ import annotations

import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

from core.settings import Environment, settings

logger = logging.getLogger(__name__)

_SERVICE_NAME = "danube-ai"


# ── LangSmith ─────────────────────────────────────────────────────────────────

def configure_langsmith() -> None:
    """Enable LangSmith tracing. No-op if LANGSMITH_API_KEY is not set."""
    if not settings.langsmith_api_key:
        os.environ["LANGSMITH_TRACING"] = "false"
        logger.info("langsmith_disabled", extra={"reason": "no api key configured"})
        return
    os.environ["LANGSMITH_TRACING"] = "true"
    os.environ["LANGSMITH_API_KEY"] = settings.langsmith_api_key
    os.environ["LANGSMITH_ENDPOINT"] = settings.langsmith_endpoint
    os.environ["LANGSMITH_PROJECT"] = settings.langsmith_project
    logger.info(
        "langsmith_configured",
        extra={"project": settings.langsmith_project, "endpoint": settings.langsmith_endpoint},
    )


# ── OpenTelemetry ─────────────────────────────────────────────────────────────

def configure_tracing() -> None:
    """Initialise OpenTelemetry. Call once at app startup."""
    resource = Resource.create({
        "service.name": _SERVICE_NAME,
        "deployment.environment": settings.environment.value,
    })
    provider = TracerProvider(resource=resource)

    if settings.environment == Environment.local:
        provider.add_span_processor(BatchSpanProcessor(ConsoleSpanExporter()))
        logger.info("otel_tracing_configured", extra={"exporter": "console"})
    else:
        from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
        otlp_exporter = OTLPSpanExporter(endpoint="http://localhost:4317", insecure=True)
        provider.add_span_processor(BatchSpanProcessor(otlp_exporter))
        logger.info("otel_tracing_configured", extra={"exporter": "otlp_datadog"})

    trace.set_tracer_provider(provider)


# Module-level tracer — use in all non-LLM spans (DB, storage, card building)
tracer = trace.get_tracer(_SERVICE_NAME)


# ── Business metrics ──────────────────────────────────────────────────────────

_meter = metrics.get_meter(_SERVICE_NAME)

_cards_served = _meter.create_counter(
    name="danube.cards_served",
    description="Number of cards served by type",
    unit="1",
)
_clarification_triggered = _meter.create_counter(
    name="danube.clarification_triggered",
    description="Number of times clarification was required",
    unit="1",
)
_request_latency = _meter.create_histogram(
    name="danube.request_latency_ms",
    description="End-to-end SSE stream latency in milliseconds",
    unit="ms",
)


def record_card_served(card_type: str, tenant_id: str) -> None:
    _cards_served.add(1, {"card_type": card_type, "tenant_id": tenant_id})


def record_clarification(intent: str, tenant_id: str) -> None:
    _clarification_triggered.add(1, {"intent": intent, "tenant_id": tenant_id})


def record_request_latency(latency_ms: float, intent: str) -> None:
    _request_latency.record(latency_ms, {"intent": intent})
