"""Gateway: the public entry point. POST raw image bytes to /process.

Flow: gateway -> resizer (/resize) -> classifier (/classify) -> store (/results) -> response.
Each upstream URL is configurable, so any stage can live on a VM, in Kubernetes or in a function.
"""
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, HTTPException, Request

from . import config
from .metrics import instrument


@asynccontextmanager
async def lifespan(app: FastAPI):
    created = False
    if getattr(app.state, "client", None) is None:   # tests inject their own client
        app.state.client = httpx.AsyncClient(
            timeout=config.UPSTREAM_TIMEOUT_S,
            limits=httpx.Limits(max_connections=500, max_keepalive_connections=config.UPSTREAM_KEEPALIVE),
        )
        created = True
    yield
    if created:
        await app.state.client.aclose()
        app.state.client = None


app = FastAPI(title="gateway", lifespan=lifespan)
instrument(app, "gateway")


async def _call(stage: str, method: str, url: str, headers: dict, **kwargs) -> httpx.Response:
    try:
        resp = await app.state.client.request(method, url, headers=headers, **kwargs)
    except httpx.TimeoutException as exc:
        raise HTTPException(504, f"{stage} timed out") from exc
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"{stage} unreachable: {exc.__class__.__name__}") from exc
    if resp.status_code >= 400:
        code = 400 if resp.status_code == 400 else 502
        raise HTTPException(code, f"{stage} returned {resp.status_code}: {resp.text[:200]}")
    return resp


@app.post("/process")
async def process(request: Request):
    start = time.perf_counter()
    data = await request.body()
    if not data:
        raise HTTPException(400, "empty body: send raw image bytes (Content-Type: image/jpeg)")
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise HTTPException(413, "image too large")
    timings = {}
    octet = {"content-type": "application/octet-stream"}

    t = time.perf_counter()
    r = await _call("resizer", "POST", f"{config.RESIZER_URL}/resize", {**octet, **config.RESIZER_HEADERS},
                    content=data)
    thumb = r.content
    timings["resize_ms"] = round((time.perf_counter() - t) * 1000, 1)

    t = time.perf_counter()
    c = await _call("classifier", "POST", f"{config.CLASSIFIER_URL}/classify",
                    {**octet, **config.CLASSIFIER_HEADERS}, content=thumb)
    result = c.json()
    timings["classify_ms"] = round((time.perf_counter() - t) * 1000, 1)

    t = time.perf_counter()
    s = await _call("store", "POST", f"{config.STORE_URL}/results", config.STORE_HEADERS,
                    json={"label": result.get("label"), "confidence": result.get("confidence"),
                          "thumbnail_bytes": len(thumb)})
    timings["store_ms"] = round((time.perf_counter() - t) * 1000, 1)

    return {
        "id": s.json().get("id"),
        "label": result.get("label"),
        "confidence": result.get("confidence"),
        "model": result.get("mode"),
        "thumbnail_bytes": len(thumb),
        "timings_ms": timings,
        "latency_ms": round((time.perf_counter() - start) * 1000, 1),
    }
