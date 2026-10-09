import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Params:
    riders: int
    rider_radius: int
    candidates: int
    alt_routes: int
    alt_penalty: float
    reliability_weight: float
    rider_min_radius: int = 8  # 外送員離店家至少這麼遠，否則幾秒內就到店

    @classmethod
    def from_env(cls) -> "Params":
        env = os.environ.get
        return cls(
            riders=int(env("RIDERS", "30")),
            rider_radius=int(env("RIDER_RADIUS", "15")),
            candidates=int(env("CANDIDATES", "5")),
            alt_routes=int(env("ALT_ROUTES", "3")),
            alt_penalty=float(env("ALT_PENALTY", "1.5")),
            reliability_weight=float(env("RELIABILITY_WEIGHT", "0.5")),
            rider_min_radius=int(env("RIDER_MIN_RADIUS", "8")),
        )


PARAMS = Params.from_env()
SERVICE = os.environ.get("K_SERVICE", "local")
ALLOWED_ORIGIN = os.environ.get("ALLOWED_ORIGIN", "")
TIME_SCALE = float(os.environ.get("TIME_SCALE", "40"))
