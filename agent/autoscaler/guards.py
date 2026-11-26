"""Safety guards between a policy's wish and what actually gets applied."""
from __future__ import annotations

import time


class Guards:
    def __init__(self, min_units: int, max_units: int, cooldown_down_s: float = 0.0, max_step: int = 2):
        self.min_units, self.max_units = min_units, max_units
        self.cooldown_down_s, self.max_step = cooldown_down_s, max_step
        self.last_up = -1e18

    def apply(self, current: int, target: int, now: float | None = None) -> tuple[int, str]:
        """Return (allowed target, note explaining any change)."""
        now = time.monotonic() if now is None else now
        notes = []
        if abs(target - current) > self.max_step:
            target = current + self.max_step * (1 if target > current else -1)
            notes.append(f"step limited to ±{self.max_step}")
        clamped = max(self.min_units, min(self.max_units, target))
        if clamped != target:
            notes.append(f"clamped to [{self.min_units},{self.max_units}]")
            target = clamped
        if target < current and now - self.last_up < self.cooldown_down_s:
            notes.append(f"scale-down held (cooldown {self.cooldown_down_s:.0f}s)")
            target = current
        if target > current:
            self.last_up = now
        return target, "; ".join(notes)
