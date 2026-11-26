"""5-minute load scenarios for the image service (same shapes as the simulator's workload patterns).

Run on the load-generator VM (not on the app or controller VM):
  SCENARIO=paper5min PEAK_USERS=120 locust -f locustfile.py --host http://<gateway> \
      --headless --csv results/<tier>_<method>_<scenario>_run1 --csv-full-history

Scenarios: paper5min (default) | steady | periodic | ramp | spike | double_spike | sawtooth
SCENARIO=manual: no load shape; the user count is set from Locust's web UI or API (the demo dashboard).
Each simulated user sends one image, waits ~1 s, repeats -> requests/s ≈ users / (1 s + latency).
"""
import io
import os
import random

from locust import HttpUser, LoadTestShape, constant, task
from PIL import Image, ImageDraw

from shapes import users_at as shape_at

SCENARIO = os.getenv("SCENARIO", "paper5min")
PEAK_USERS = int(os.getenv("PEAK_USERS", "100"))
DURATION_S = int(os.getenv("DURATION_S", "300"))
SEED = int(os.getenv("SEED", "42"))


def _make_images(n: int = 50, seed: int = SEED) -> list[bytes]:
    """Synthetic photos (random sizes, shapes and colours) so no dataset download is needed."""
    rng = random.Random(seed)
    images = []
    for _ in range(n):
        w, h = rng.choice([(640, 480), (800, 600), (1024, 768), (1280, 720), (600, 600)])
        img = Image.new("RGB", (w, h), tuple(rng.randint(0, 255) for _ in range(3)))
        draw = ImageDraw.Draw(img)
        for _ in range(rng.randint(5, 25)):
            box = sorted(rng.sample(range(w), 2)), sorted(rng.sample(range(h), 2))
            shape = (box[0][0], box[1][0], box[0][1], box[1][1])
            colour = tuple(rng.randint(0, 255) for _ in range(3))
            (draw.ellipse if rng.random() < 0.5 else draw.rectangle)(shape, fill=colour)
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=rng.randint(70, 92))
        images.append(buf.getvalue())
    return images


IMAGES = _make_images()


def users_at(t: float) -> float:
    """Fraction of PEAK_USERS at second t (shapes live in shapes.py, shared with the demo dashboard)."""
    return shape_at(t, SCENARIO, DURATION_S)


class ImageUser(HttpUser):
    wait_time = constant(1)

    @task
    def process(self):
        with self.client.post("/process", data=random.choice(IMAGES), headers={"Content-Type": "image/jpeg"},
                              name="/process", catch_response=True) as resp:
            if resp.status_code != 200:
                resp.failure(f"HTTP {resp.status_code}")
            else:
                resp.success()


if SCENARIO != "manual":
    class ScenarioShape(LoadTestShape):
        def tick(self):
            t = self.get_run_time()
            if t >= DURATION_S:
                return None
            users = max(1, round(PEAK_USERS * users_at(t)))
            return users, max(10, users)      # (target users, spawn rate per second) -> near-instant changes
