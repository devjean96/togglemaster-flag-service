from opentelemetry import propagate
from opentelemetry.instrumentation.requests import RequestsInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from requests import Response, Session
from requests.adapters import BaseAdapter

from telemetry import _resource_attributes


class CaptureAdapter(BaseAdapter):
    def __init__(self):
        self.headers = None

    def send(self, request, **kwargs):
        self.headers = request.headers
        response = Response()
        response.status_code = 200
        response.url = request.url
        response.request = request
        response._content = b"{}"
        return response

    def close(self):
        pass


def test_resource_attributes_include_gitops_metadata(monkeypatch):
    monkeypatch.setenv("OTEL_SERVICE_NAME", "flag-service")
    monkeypatch.setenv(
        "OTEL_RESOURCE_ATTRIBUTES",
        "service.namespace=togglemaster,deployment.environment.name=homolog,service.version=sha-abc123",
    )

    attributes = _resource_attributes("fallback")

    assert attributes == {
        "service.name": "flag-service",
        "service.namespace": "togglemaster",
        "deployment.environment.name": "homolog",
        "service.version": "sha-abc123",
    }


def test_requests_instrumentation_propagates_traceparent(monkeypatch):
    monkeypatch.setenv("OTEL_SDK_DISABLED", "false")
    provider = TracerProvider()
    propagate.set_global_textmap(TraceContextTextMapPropagator())
    instrumentor = RequestsInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    adapter = CaptureAdapter()
    session = Session()
    session.mount("http://", adapter)

    try:
        with provider.get_tracer("test/flag").start_as_current_span("parent"):
            session.get("http://auth-service/validate")
    finally:
        instrumentor.uninstrument()
        provider.shutdown()

    assert adapter.headers["traceparent"].startswith("00-")
