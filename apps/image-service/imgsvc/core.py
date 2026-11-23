"""The actual work: image resizing and image classification.

Shared by the HTTP services (VM / Kubernetes) and the function wrappers (Lambda / Azure / GCP),
so every tier runs identical logic and the comparison between tiers is fair.
"""
import io
import logging
import threading

import numpy as np
from PIL import Image, ImageFilter

from . import config

log = logging.getLogger("imgsvc.core")

IMAGENET_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
IMAGENET_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)


def load_image(data: bytes) -> Image.Image:
    if len(data) > config.MAX_UPLOAD_BYTES:
        raise ValueError("image too large")
    img = Image.open(io.BytesIO(data))
    img.load()
    return img.convert("RGB")


def resize_image(data: bytes, size: int | None = None, quality: int = 85) -> bytes:
    """Centre-crop to a square, resize to `size` x `size`, sharpen, return JPEG bytes."""
    size = size or config.THUMB_SIZE
    img = load_image(data)
    w, h = img.size
    s = min(w, h)
    left, top = (w - s) // 2, (h - s) // 2
    img = img.crop((left, top, left + s, top + s)).resize((size, size), Image.LANCZOS)
    img = img.filter(ImageFilter.SHARPEN)
    out = io.BytesIO()
    img.save(out, format="JPEG", quality=quality)
    return out.getvalue()


class Classifier:
    """MobileNetV2 (ONNX, CPU). Falls back to a CPU-bound synthetic model if the file is missing,
    so tests and CI work without downloading the model."""

    def __init__(self, model_path: str, labels_path: str, threads: int = 1):
        self.session = None
        self.input_name = None
        self.labels: list[str] = []
        try:
            import onnxruntime as ort

            opts = ort.SessionOptions()
            opts.intra_op_num_threads = threads
            opts.inter_op_num_threads = 1
            self.session = ort.InferenceSession(model_path, sess_options=opts, providers=["CPUExecutionProvider"])
            self.input_name = self.session.get_inputs()[0].name
        except Exception as exc:  # noqa: BLE001 - any failure -> synthetic mode
            log.warning("ONNX model not loaded (%s); using synthetic classifier", exc)
        try:
            with open(labels_path, encoding="utf-8") as f:
                self.labels = [line.strip() for line in f if line.strip()]
        except OSError:
            pass
        rng = np.random.default_rng(0)
        self._w_in = rng.standard_normal((512, 3 * 32 * 32), dtype=np.float32) / 55.0
        self._w_hidden = rng.standard_normal((512, 512), dtype=np.float32) / 22.0
        self._w_out = rng.standard_normal((1000, 512), dtype=np.float32) / 22.0

    @property
    def mode(self) -> str:
        return "onnx" if self.session is not None else "synthetic"

    @staticmethod
    def preprocess(data: bytes) -> np.ndarray:
        img = load_image(data).resize((224, 224), Image.BILINEAR)
        x = (np.asarray(img, dtype=np.float32) / 255.0 - IMAGENET_MEAN) / IMAGENET_STD
        return x.transpose(2, 0, 1)[None].astype(np.float32)   # NCHW

    def _synthetic_logits(self, x: np.ndarray, rounds: int = 60) -> np.ndarray:
        small = x[0][:, ::7, ::7][:, :32, :32].reshape(-1)
        v = np.tanh(self._w_in @ small)
        for _ in range(rounds):          # CPU work roughly comparable to a small CNN
            v = np.tanh(self._w_hidden @ v)
        return self._w_out @ v

    def classify(self, data: bytes) -> dict:
        x = self.preprocess(data)
        if self.session is not None:
            logits = self.session.run(None, {self.input_name: x})[0].reshape(-1)
        else:
            logits = self._synthetic_logits(x)
        logits = logits - logits.max()
        probs = np.exp(logits) / np.exp(logits).sum()
        idx = int(probs.argmax())
        label = self.labels[idx] if idx < len(self.labels) else f"class_{idx}"
        return {"label": label, "confidence": round(float(probs[idx]), 4), "class_id": idx, "mode": self.mode}


_classifier: Classifier | None = None
_lock = threading.Lock()


def get_classifier() -> Classifier:
    global _classifier
    if _classifier is None:
        with _lock:
            if _classifier is None:
                _classifier = Classifier(config.MODEL_PATH, config.LABELS_PATH, config.ORT_THREADS)
    return _classifier
