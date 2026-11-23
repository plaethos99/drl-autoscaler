import io
import os
import sys

import pytest
from PIL import Image

# Tests run without the ONNX model (synthetic classifier) and without Redis (memory store).
os.environ.setdefault("MODEL_PATH", "/nonexistent/model.onnx")
os.environ.setdefault("LABELS_PATH", "/nonexistent/labels.txt")
os.environ.setdefault("REDIS_URL", "")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


@pytest.fixture
def jpeg_bytes() -> bytes:
    img = Image.new("RGB", (640, 480), (200, 60, 30))
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()
