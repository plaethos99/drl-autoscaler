"""Prometheus metrics + /health + /metrics, shared by every HTTP service.

The RL agent reads these through Prometheus, e.g.
  p95 latency : histogram_quantile(0.95, sum(rate(app_request_latency_seconds_bucket{service="gateway"}[30s])) by (le))
  request rate: sum(rate(app_requests_total{service="gateway"}[30s]))
  queue proxy : sum(app_inflight_requests{service="classifier"})
"""
import time

from fastapi import FastAPI, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from . import config

BUCKETS = (0.025, 0.05, 0.1, 0.2, 0.3, 0.4, 0.5, 0.75, 1.0, 1.5, 2.0, 3.0, 5.0, 10.0)

REQUESTS = Counter("app_requests_total", "Requests handled", ["service", "endpoint", "status"])
LATENCY = Histogram("app_request_latency_seconds", "Request latency (includes queueing)", ["service", "endpoint"],
                    buckets=BUCKETS)
INFLIGHT = Gauge("app_inflight_requests", "Requests currently being processed or queued", ["service"])
SLA_VIOLATIONS = Counter("app_sla_violations_total", "Requests slower than the SLA", ["service"])

_SKIP = {"/metrics", "/health"}


def instrument(app: FastAPI, service: str) -> None:
    @app.middleware("http")
    async def _measure(request: Request, call_next):
        path = request.url.path
        if path in _SKIP:
            return await call_next(request)
        INFLIGHT.labels(service).inc()
        start, status = time.perf_counter(), "500"
        try:
            response = await call_next(request)
            status = str(response.status_code)
            return response
        finally:
            elapsed = time.perf_counter() - start
            INFLIGHT.labels(service).dec()
            REQUESTS.labels(service, path, status).inc()
            LATENCY.labels(service, path).observe(elapsed)
            if elapsed * 1000.0 > config.SLA_MS:
                SLA_VIOLATIONS.labels(service).inc()

    @app.get("/health")
    def health():
        return {"status": "ok", "service": service}

    @app.get("/metrics")
    def metrics():
        return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
