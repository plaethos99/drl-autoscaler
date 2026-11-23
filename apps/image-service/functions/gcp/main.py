"""Cloud Run functions (functions-framework): deploy twice, entry points `resize` and `classify`.

Gateway setting: RESIZER_URL / CLASSIFIER_URL = the function's HTTPS URL (any sub-path is accepted).
Package with build.sh (copies imgsvc/ and the model next to this file).
"""
import json
import os

import functions_framework

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ.setdefault("MODEL_PATH", os.path.join(HERE, "models", "mobilenetv2-12.onnx"))
os.environ.setdefault("LABELS_PATH", os.path.join(HERE, "models", "imagenet_classes.txt"))

from imgsvc import core  # noqa: E402  (env must be set first)

JSON = {"Content-Type": "application/json"}


@functions_framework.http
def resize(request):
    data = request.get_data()
    if not data:
        return json.dumps({"detail": "empty body: send raw image bytes"}), 400, JSON
    try:
        return core.resize_image(data), 200, {"Content-Type": "image/jpeg"}
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"detail": f"invalid image: {exc}"}), 400, JSON


@functions_framework.http
def classify(request):
    data = request.get_data()
    if not data:
        return json.dumps({"detail": "empty body: send raw image bytes"}), 400, JSON
    try:
        return json.dumps(core.get_classifier().classify(data)), 200, JSON
    except Exception as exc:  # noqa: BLE001
        return json.dumps({"detail": f"invalid image: {exc}"}), 400, JSON
