"""Azure Functions (Python v2 model): two HTTP functions, /api/resize and /api/classify.

Gateway setting: RESIZER_URL / CLASSIFIER_URL = https://<app>.azurewebsites.net/api
and RESIZER_HEADERS / CLASSIFIER_HEADERS = {"x-functions-key": "<function key>"}.
Package with build.sh (copies imgsvc/ and the model next to this file).
"""
import json
import os

import azure.functions as func

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("MODEL_PATH", os.path.join(HERE, "models", "mobilenetv2-12.onnx"))
os.environ.setdefault("LABELS_PATH", os.path.join(HERE, "models", "imagenet_classes.txt"))

from imgsvc import core  # noqa: E402  (env must be set first)

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)


def _err(code: int, msg: str) -> func.HttpResponse:
    return func.HttpResponse(json.dumps({"detail": msg}), status_code=code, mimetype="application/json")


@app.route(route="resize", methods=["POST"])
def resize(req: func.HttpRequest) -> func.HttpResponse:
    data = req.get_body()
    if not data:
        return _err(400, "empty body: send raw image bytes")
    try:
        return func.HttpResponse(core.resize_image(data), mimetype="image/jpeg")
    except Exception as exc:  # noqa: BLE001
        return _err(400, f"invalid image: {exc}")


@app.route(route="classify", methods=["POST"])
def classify(req: func.HttpRequest) -> func.HttpResponse:
    data = req.get_body()
    if not data:
        return _err(400, "empty body: send raw image bytes")
    try:
        return func.HttpResponse(json.dumps(core.get_classifier().classify(data)), mimetype="application/json")
    except Exception as exc:  # noqa: BLE001
        return _err(400, f"invalid image: {exc}")
