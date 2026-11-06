import atexit
import os
import threading

from opentelemetry import metrics, trace
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.instrumentation.flask import FlaskInstrumentor
from opentelemetry.instrumentation.psycopg2 import Psycopg2Instrumentor
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

_initialization_lock = threading.Lock()
_initialized = False


def _resource_attributes(default_service_name):
    attributes = {"service.name": os.getenv("OTEL_SERVICE_NAME", default_service_name)}
    raw_attributes = os.getenv("OTEL_RESOURCE_ATTRIBUTES", "")
    for item in raw_attributes.split(","):
        key, separator, value = item.partition("=")
        if separator and key.strip() and value.strip():
            attributes[key.strip()] = value.strip()
    return attributes


def init_telemetry(flask_app, default_service_name):
    """Inicializa providers e instrumentações uma vez por processo Gunicorn."""
    global _initialized

    if os.getenv("OTEL_SDK_DISABLED", "false").lower() == "true":
        return

    with _initialization_lock:
        if _initialized:
            return

        resource = Resource.create(_resource_attributes(default_service_name))

        tracer_provider = TracerProvider(resource=resource)
        tracer_provider.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
        trace.set_tracer_provider(tracer_provider)

        metric_reader = PeriodicExportingMetricReader(OTLPMetricExporter())
        meter_provider = MeterProvider(resource=resource, metric_readers=[metric_reader])
        metrics.set_meter_provider(meter_provider)

        FlaskInstrumentor().instrument_app(flask_app, excluded_urls="health")
        RequestsInstrumentor().instrument()
        Psycopg2Instrumentor().instrument(enable_commenter=False)

        def shutdown():
            meter_provider.shutdown()
            tracer_provider.shutdown()

        atexit.register(shutdown)
        _initialized = True
