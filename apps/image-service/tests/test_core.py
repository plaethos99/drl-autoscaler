import io

import pytest
from PIL import Image

from imgsvc import core


def test_resize_returns_square_jpeg(jpeg_bytes):
    out = core.resize_image(jpeg_bytes)
    img = Image.open(io.BytesIO(out))
    assert img.format == "JPEG"
    assert img.size == (224, 224)


def test_resize_rejects_garbage():
    with pytest.raises(Exception):
        core.resize_image(b"not an image")


def test_classifier_synthetic_mode(jpeg_bytes):
    result = core.get_classifier().classify(jpeg_bytes)
    assert result["mode"] == "synthetic"
    assert 0.0 <= result["confidence"] <= 1.0
    assert 0 <= result["class_id"] < 1000
    assert result["label"]
