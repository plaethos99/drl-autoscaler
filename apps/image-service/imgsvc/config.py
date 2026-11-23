"""All settings come from environment variables so the same code runs on VM, K8s and functions."""
import json
import os


def _headers(name: str) -> dict:
    """Optional auth headers for an upstream, e.g. CLASSIFIER_HEADERS='{"x-functions-key": "..."}'."""
    raw = os.getenv(name, "")
    return json.loads(raw) if raw else {}


SERVICE = os.getenv("SERVICE", "unknown")

# Upstream base URLs used by the gateway. Point one at a function URL for the combined tier.
RESIZER_URL = os.getenv("RESIZER_URL", "http://resizer:8000").rstrip("/")
CLASSIFIER_URL = os.getenv("CLASSIFIER_URL", "http://classifier:8000").rstrip("/")
STORE_URL = os.getenv("STORE_URL", "http://store:8000").rstrip("/")
RESIZER_HEADERS = _headers("RESIZER_HEADERS")
CLASSIFIER_HEADERS = _headers("CLASSIFIER_HEADERS")
STORE_HEADERS = _headers("STORE_HEADERS")
UPSTREAM_TIMEOUT_S = float(os.getenv("UPSTREAM_TIMEOUT_S", "10"))

REDIS_URL = os.getenv("REDIS_URL", "")          # empty -> in-memory store
RESULT_TTL_S = int(os.getenv("RESULT_TTL_S", "3600"))

MODEL_PATH = os.getenv("MODEL_PATH", "/models/mobilenetv2-12.onnx")
LABELS_PATH = os.getenv("LABELS_PATH", "/models/imagenet_classes.txt")
ORT_THREADS = int(os.getenv("ORT_THREADS", "1"))  # 1 thread per replica -> predictable per-replica capacity
THUMB_SIZE = int(os.getenv("THUMB_SIZE", "224"))
MAX_UPLOAD_BYTES = int(os.getenv("MAX_UPLOAD_BYTES", str(8 * 1024 * 1024)))

SLA_MS = float(os.getenv("SLA_MS", "500"))       # p95 target used in the paper
