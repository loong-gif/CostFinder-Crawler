"""
OpenTelemetry Superlog observability bootstrap for costfinder.

Provides:
  - init_observability()   → configure traces, metrics, and log export
  - tracer                 → module-scope Tracer for custom spans
  - meter                  → module-scope Meter for custom metrics

Usage:
    from utils.observability import init_observability, tracer, meter
    init_observability()
"""
from __future__ import annotations

import logging
import sys
from typing import Callable, Dict

# OpenTelemetry 仅用于遥测上报，对业务逻辑非必需。若运行环境未安装 otel
# （例如 opentelemetry>=1.42 需 Python>=3.10，而某些解释器为 3.9），则优雅降级：
# 提供 no-op 的 tracer/meter，init_observability() 变为安全空操作，
# 避免在模块导入期直接 crash 拖垮整个脚本。
try:
    # ── OTel core ───────────────────────────────────────────────────────
    from opentelemetry import metrics, trace
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    # ── OTel exporters (HTTP / protobuf) ────────────────────────────────
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

    # ── OTel logging bridge ─────────────────────────────────────────────
    from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    _OTEL_AVAILABLE = True
except ImportError:
    _OTEL_AVAILABLE = False


# ────────────────────────────────────────────────────────────────────────
# Superlog endpoint & public token
# ────────────────────────────────────────────────────────────────────────
SUPERLOG_ENDPOINT = "https://intake.superlog.sh"
SUPERLOG_PUBLIC_TOKEN = "sl_public_QqoHtWP3QEuqp86o2QOGFVRax0zKUgt1VLdYyQIPiII"


def superlog_headers() -> Dict[str, str]:
    """Return the authorization headers required by the Superlog intake."""
    return {"Authorization": f"Bearer {SUPERLOG_PUBLIC_TOKEN}"}


# ────────────────────────────────────────────────────────────────────────
# No-op fallbacks used when OpenTelemetry is unavailable. They implement the
# minimal surface callers might touch (span context managers, metric records)
# so business code never has to branch on whether otel is installed.
# ────────────────────────────────────────────────────────────────────────
class _NoopSpan:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def set_attribute(self, *args, **kwargs):
        pass

    def set_status(self, *args, **kwargs):
        pass

    def record_exception(self, *args, **kwargs):
        pass

    def add_event(self, *args, **kwargs):
        pass

    def end(self, *args, **kwargs):
        pass


class _NoopTracer:
    def start_as_current_span(self, *args, **kwargs):
        return _NoopSpan()

    def start_span(self, *args, **kwargs):
        return _NoopSpan()


class _NoopInstrument:
    def add(self, *args, **kwargs):
        pass

    def record(self, *args, **kwargs):
        pass


class _NoopMeter:
    def create_counter(self, *args, **kwargs):
        return _NoopInstrument()

    def create_up_down_counter(self, *args, **kwargs):
        return _NoopInstrument()

    def create_histogram(self, *args, **kwargs):
        return _NoopInstrument()

    def create_observable_gauge(self, *args, **kwargs):
        return _NoopInstrument()

    def create_observable_counter(self, *args, **kwargs):
        return _NoopInstrument()


# ────────────────────────────────────────────────────────────────────────
# Shared resource attributes + module-scope tracer & meter.
# (Real export only starts once init_observability() is called; when otel is
# missing these stay as no-ops for the whole process.)
# ────────────────────────────────────────────────────────────────────────
if _OTEL_AVAILABLE:
    _RESOURCE = Resource.create(
        {
            "service.name": "costfinder",
            "deployment.environment.name": "local",
            "vcs.repository.url.full": "https://github.com/superloglabs/skills",
        }
    )
    tracer = trace.get_tracer("costfinder")
    meter = metrics.get_meter("costfinder")
else:
    _RESOURCE = None
    tracer = _NoopTracer()
    meter = _NoopMeter()

_initialized = False


def _bridge_loguru_to_otel(handler: LoggingHandler) -> Callable:
    """
    Return a loguru-compatible sink that forwards records through the
    OTel LoggingHandler so they are exported via OTLP.

    loguru sinks receive (message) objects; we build a real
    logging.LogRecord and pass it to the handler.
    """
    import logging as _logging

    def _sink(message):
        record = message.record
        level = record["level"].name  # e.g. "INFO", "WARNING"
        # Map loguru level to stdlib logging level
        stdlib_level = _logging.getLevelName(level.upper())
        if isinstance(stdlib_level, str):
            # getLevelName returns a string when it can't map;
            # fall back to INFO
            stdlib_level = _logging.INFO

        log_record = _logging.LogRecord(
            name=record["name"] or "costfinder",
            level=stdlib_level,
            pathname=record["file"].path if hasattr(record["file"], "path") else str(record["file"]),
            lineno=record["line"],
            msg=record["message"],
            args=None,
            exc_info=None,
        )
        # Attach OTel trace/span context so logs are correlated
        handler.emit(log_record)

    return _sink


def init_observability() -> None:
    """
    Bootstrap all three OTel signals (traces, metrics, logs) with
    HTTP/protobuf exporters pointed at the Superlog intake.

    Safe to call multiple times; only the first call takes effect.

    No-op when OpenTelemetry is not installed (telemetry is non-essential).
    """
    global _initialized
    if _initialized:
        return
    _initialized = True

    if not _OTEL_AVAILABLE:
        logging.getLogger("costfinder").info(
            "OpenTelemetry not installed; observability disabled (telemetry only, no business impact)."
        )
        return

    headers = superlog_headers()

    # ── Traces ──────────────────────────────────────────────────────────
    span_exporter = OTLPSpanExporter(
        endpoint=f"{SUPERLOG_ENDPOINT}/v1/traces",
        headers=headers,
    )
    tracer_provider = TracerProvider(resource=_RESOURCE)
    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    trace.set_tracer_provider(tracer_provider)

    # ── Metrics ─────────────────────────────────────────────────────────
    metric_exporter = OTLPMetricExporter(
        endpoint=f"{SUPERLOG_ENDPOINT}/v1/metrics",
        headers=headers,
    )
    metric_reader = PeriodicExportingMetricReader(metric_exporter)
    meter_provider = MeterProvider(resource=_RESOURCE, metric_readers=[metric_reader])
    metrics.set_meter_provider(meter_provider)

    # ── Logs ────────────────────────────────────────────────────────────
    log_exporter = OTLPLogExporter(
        endpoint=f"{SUPERLOG_ENDPOINT}/v1/logs",
        headers=headers,
    )
    logger_provider = LoggerProvider(resource=_RESOURCE)
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))

    # stdlib logging handler (for any library using stdlib logging)
    otel_handler = LoggingHandler(level=logging.DEBUG, logger_provider=logger_provider)
    stdlib_logger = logging.getLogger("costfinder")
    stdlib_logger.addHandler(otel_handler)
    stdlib_logger.setLevel(logging.DEBUG)

    # Bridge loguru → OTLP
    try:
        from loguru import logger as loguru_logger
        loguru_sink = _bridge_loguru_to_otel(otel_handler)
        loguru_logger.add(loguru_sink, level="DEBUG", format="{message}")
    except ImportError:
        pass  # loguru not available; skip bridge

    # Reassign module-scope tracer/meter so callers get the real providers
    global tracer, meter
    tracer = trace.get_tracer("costfinder")
    meter = metrics.get_meter("costfinder")
