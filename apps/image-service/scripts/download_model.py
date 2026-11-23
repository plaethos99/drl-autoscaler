"""Download MobileNetV2 (ONNX) and the ImageNet labels at image build time.

Usage: python download_model.py /models
Fails the build if the download fails, so experiments never run on the synthetic fallback by accident
(set ALLOW_SYNTHETIC=1 to continue anyway).
"""
import os
import sys
import urllib.request

FILES = {
    "mobilenetv2-12.onnx": "https://github.com/onnx/models/raw/main/validated/vision/classification/mobilenet/model/mobilenetv2-12.onnx",
    "imagenet_classes.txt": "https://raw.githubusercontent.com/pytorch/hub/master/imagenet_classes.txt",
}
MIN_BYTES = {"mobilenetv2-12.onnx": 5_000_000, "imagenet_classes.txt": 5_000}


def main(dest: str) -> int:
    os.makedirs(dest, exist_ok=True)
    ok = True
    for name, url in FILES.items():
        path = os.path.join(dest, name)
        try:
            urllib.request.urlretrieve(url, path)
            size = os.path.getsize(path)
            if size < MIN_BYTES[name]:
                raise RuntimeError(f"file too small ({size} bytes) - probably an LFS pointer or error page")
            print(f"downloaded {name} ({size / 1e6:.1f} MB)")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"FAILED {name}: {exc}", file=sys.stderr)
            if os.path.exists(path):
                os.remove(path)
    if not ok and os.getenv("ALLOW_SYNTHETIC") != "1":
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else "/models"))
