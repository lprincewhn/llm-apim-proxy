"""Deterministic routing health state; no implicit recovery on missing samples."""

from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class Health:
    state: str = "Healthy"
    bad_windows: int = 0
    good_windows: int = 0
    quarantined_until: float = 0


def evaluate(previous, *, samples, p95_ms, error_rate, budget_ms, now,
             probe_ok: Optional[bool] = None):
    health = Health(**previous)
    if health.state == "ManualDisabled":
        return asdict(health)
    if health.state == "Quarantined":
        if now < health.quarantined_until:
            return asdict(health)
        health.good_windows = health.good_windows + 1 if probe_ok is True else 0
        if health.good_windows >= 5:
            health.state = "Recovering"
        return asdict(health)
    if samples < 100:
        return asdict(health)
    unhealthy = error_rate >= 0.05 or p95_ms > budget_ms * 0.8
    health.bad_windows = health.bad_windows + 1 if unhealthy else 0
    health.good_windows = 0 if unhealthy else health.good_windows + 1
    if health.bad_windows >= 2:
        health.state = "Quarantined"
        health.quarantined_until = now + 300
    elif unhealthy:
        health.state = "Suspect"
    elif health.good_windows >= 5:
        health.state = "Healthy"
    return asdict(health)
