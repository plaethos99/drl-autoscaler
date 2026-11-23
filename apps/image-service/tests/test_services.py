import httpx
from fastapi.testclient import TestClient

from imgsvc import classifier, gateway, resizer, store


def test_resizer_endpoint(jpeg_bytes):
    with TestClient(resizer.app) as c:
        r = c.post("/resize", content=jpeg_bytes)
        assert r.status_code == 200
        assert r.headers["content-type"] == "image/jpeg"
        assert c.post("/resize", content=b"").status_code == 400
        assert c.get("/health").json()["service"] == "resizer"


def test_classifier_endpoint(jpeg_bytes):
    with TestClient(classifier.app) as c:
        r = c.post("/classify", content=jpeg_bytes)
        assert r.status_code == 200
        assert {"label", "confidence", "class_id", "mode"} <= r.json().keys()
        assert c.post("/classify", content=b"junk").status_code == 400


def test_store_roundtrip():
    with TestClient(store.app) as c:
        saved = c.post("/results", json={"label": "cat"}).json()
        assert c.get(f"/results/{saved['id']}").json()["label"] == "cat"
        assert c.get("/stats").json()["results"] >= 1
        assert c.get("/results/missing").status_code == 404


def test_metrics_exposed(jpeg_bytes):
    with TestClient(resizer.app) as c:
        c.post("/resize", content=jpeg_bytes)
        body = c.get("/metrics").text
        assert "app_request_latency_seconds_bucket" in body
        assert 'service="resizer"' in body


class RouterTransport(httpx.AsyncBaseTransport):
    """Routes the gateway's upstream calls (http://resizer:8000 ...) to in-process ASGI apps."""

    def __init__(self, apps):
        self.routes = {host: httpx.ASGITransport(app=app) for host, app in apps.items()}

    async def handle_async_request(self, request):
        return await self.routes[request.url.host].handle_async_request(request)


def test_gateway_end_to_end(jpeg_bytes):
    with TestClient(store.app):                 # runs store's lifespan (creates its backend)
        gateway.app.state.client = httpx.AsyncClient(
            transport=RouterTransport({"resizer": resizer.app, "classifier": classifier.app, "store": store.app}))
        try:
            with TestClient(gateway.app) as c:
                r = c.post("/process", content=jpeg_bytes, headers={"Content-Type": "image/jpeg"})
                assert r.status_code == 200, r.text
                body = r.json()
                assert body["thumbnail_bytes"] > 0
                assert body["label"]
                assert set(body["timings_ms"]) == {"resize_ms", "classify_ms", "store_ms"}
                assert c.post("/process", content=b"").status_code == 400
                assert c.post("/process", content=b"junk").status_code == 400
        finally:
            gateway.app.state.client = None
