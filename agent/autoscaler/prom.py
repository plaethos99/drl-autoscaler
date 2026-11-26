"""Read the live metrics the policy needs from Prometheus (one HTTP query per value)."""
from __future__ import annotations

import math
from dataclasses import dataclass

import requests


class PromError(RuntimeError):
    pass


@dataclass
class Metrics:
    rps: float              # requests/s arriving at the gateway
    p95_ms: float           # gateway p95 latency (end-to-end, what the SLA is about)
    inflight: float         # requests queued or in progress in the classifier
    cpu_cores: float        # classifier CPU use (cores), from cAdvisor
    mem_bytes: float        # classifier working-set memory (logged only)


def queries(window: str, container: str) -> dict[str, str]:
    return {
        "rps": f'sum(rate(app_requests_total{{service="gateway",endpoint="/process"}}[{window}]))',
        "p95_ms": ("1000 * histogram_quantile(0.95, sum(rate(app_request_latency_seconds_bucket"
                   f'{{service="gateway",endpoint="/process"}}[{window}])) by (le))'),
        "inflight": 'sum(app_inflight_requests{service="classifier"})',
        "cpu_cores": f'sum(rate(container_cpu_usage_seconds_total{{name="{container}"}}[{window}]))',
        "mem_bytes": f'sum(container_memory_working_set_bytes{{name="{container}"}})',
    }


class Prometheus:
    def __init__(self, url: str, window: str, container: str, timeout_s: float = 2.0):
        self.url = url.rstrip("/") + "/api/v1/query"
        self.q = queries(window, container)
        self.timeout_s = timeout_s
        self.session = requests.Session()

    def scalar(self, query: str) -> float:
        try:
            r = self.session.get(self.url, params={"query": query}, timeout=self.timeout_s)
            r.raise_for_status()
            body = r.json()
        except (requests.RequestException, ValueError) as exc:
            raise PromError(f"query failed: {exc}") from exc
        if body.get("status") != "success":
            raise PromError(f"query error: {body.get('error')}")
        result = body["data"]["result"]
        if not result:
            return math.nan        # no series yet (e.g. no traffic): caller decides what that means
        return float(result[0]["value"][1])

    def read(self) -> Metrics:
        v = {k: self.scalar(q) for k, q in self.q.items()}
        # No traffic -> rate/histogram are empty: treat as zero load, base latency unknown (0).
        rps = 0.0 if math.isnan(v["rps"]) else v["rps"]
        p95 = v["p95_ms"]
        if math.isnan(p95) or math.isinf(p95):
            p95 = 0.0 if rps < 0.05 else math.nan     # traffic but no latency -> a real read problem
        if math.isnan(v["cpu_cores"]):
            raise PromError("no CPU series for the target container (is cAdvisor running?)")
        return Metrics(rps=rps, p95_ms=p95,
                       inflight=0.0 if math.isnan(v["inflight"]) else v["inflight"],
                       cpu_cores=v["cpu_cores"],
                       mem_bytes=0.0 if math.isnan(v["mem_bytes"]) else v["mem_bytes"])
