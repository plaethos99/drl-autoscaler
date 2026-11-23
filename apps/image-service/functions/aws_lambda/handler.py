"""AWS Lambda wrapper (container image). ROLE=resizer|classifier selects the function.

Invoked through a Function URL or API Gateway (HTTP API, payload v2). The gateway service calls
<base-url>/resize or <base-url>/classify; the path is ignored here, ROLE decides what runs.
"""
import base64
import json
import os

from imgsvc import core

ROLE = os.environ.get("ROLE", "classifier")
if ROLE == "classifier":
    core.get_classifier()   # load the model during the cold start, like the HTTP service does


def _body(event: dict) -> bytes:
    body = event.get("body") or ""
    return base64.b64decode(body) if event.get("isBase64Encoded") else body.encode("latin-1")


def _error(code: int, msg: str) -> dict:
    return {"statusCode": code, "headers": {"content-type": "application/json"}, "body": json.dumps({"detail": msg})}


def handler(event, _context):
    data = _body(event)
    if not data:
        return _error(400, "empty body: send raw image bytes")
    try:
        if ROLE == "resizer":
            thumb = core.resize_image(data)
            return {"statusCode": 200, "headers": {"content-type": "image/jpeg"},
                    "body": base64.b64encode(thumb).decode(), "isBase64Encoded": True}
        result = core.get_classifier().classify(data)
        return {"statusCode": 200, "headers": {"content-type": "application/json"}, "body": json.dumps(result)}
    except Exception as exc:  # noqa: BLE001
        return _error(400, f"invalid image: {exc}")
