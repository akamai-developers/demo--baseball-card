"""Pydantic models for the stats a card is made from, a card job, and health reports."""

from __future__ import annotations

import math
import secrets
from typing import Annotated, Literal, NamedTuple

from pydantic import BaseModel, ConfigDict, Field, field_validator

JobStatus = Literal["queued", "running", "done", "failed"]
StepStatus = Literal["queued", "running", "done", "failed", "skipped"]

# The two card designs, in packages/card/src/card/styles.json. A style is a
# template and a prompt for the background, nothing more.
StyleName = Literal["akamai", "classic"]
STYLES: tuple[str, ...] = ("akamai", "classic")


class Metric(NamedTuple):
    """One swing number the card knows how to show."""

    # What the card back calls it, and the unit after the number.
    label: str
    unit: str
    # How many decimal places the back prints.
    decimals: int
    # The range the api accepts. It brackets the qualified batters in the
    # Statcast table, widened so a recreational swing still fits.
    low: float
    high: float


METRICS: dict[str, Metric] = {
    "bat_speed_mph": Metric("Bat Speed", "mph", 1, 50, 85),
    "exit_velo_mph": Metric("Exit Velocity", "mph", 1, 60, 115),
    "launch_angle_deg": Metric("Launch Angle", "deg", 1, -10, 35),
    "swing_length_ft": Metric("Swing Length", "ft", 1, 4.5, 10),
    "attack_angle_deg": Metric("Attack Angle", "deg", 1, -5, 25),
    "time_to_contact_ms": Metric("Time to Contact", "ms", 0, 100, 220),
}
SWING_METRICS: tuple[str, ...] = tuple(METRICS)


def new_card_id() -> str:
    """A 128-bit URL-safe token. 22 characters, no padding."""
    return secrets.token_urlsafe(16)


class Stats(BaseModel):
    """What a card is made from, besides the photo. Only name is required."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    name: Annotated[str, Field(min_length=1, max_length=60)]
    number: int | None = Field(default=None, ge=0, le=99)
    position: str | None = Field(default=None, max_length=4)
    bats: Literal["R", "L", "S"] | None = None
    throws: Literal["R", "L"] | None = None
    height_in: int | None = Field(default=None, ge=36, le=96)
    weight_lb: int | None = Field(default=None, ge=60, le=500)
    hometown: str | None = Field(default=None, max_length=60)
    metrics: dict[str, float] = Field(default_factory=dict)

    @field_validator("metrics")
    @classmethod
    def _known_and_in_range(cls, value: dict[str, float]) -> dict[str, float]:
        """Each number is one the card knows, and inside its range. The message
        says which one and what range, so a person can fix it."""
        for key, number in value.items():
            metric = METRICS.get(key)
            if metric is None:
                raise ValueError(f"{key} is not a number this card shows. Use {', '.join(METRICS)}")
            if not math.isfinite(number):
                raise ValueError(f"{metric.label} is not a number")
            if not metric.low <= number <= metric.high:
                raise ValueError(
                    f"{metric.label} is {number:g} {metric.unit}. "
                    f"It has to be from {metric.low:g} to {metric.high:g} {metric.unit}"
                )
        return value


class Step(BaseModel):
    """One row of the pipeline ledger. It ends up on the back of the card."""

    step: str
    status: StepStatus = "queued"
    model: str | None = None
    license: str | None = None
    # akamai-gpu0, akamai-gpu1, akamai-cpu, or a mock- label on a laptop.
    # Reported by the service itself.
    ran_on: str | None = None
    ms: int | None = None
    error: str | None = None
    # The art step: "pool" for a background painted before the card asked,
    # "fresh" for one painted for this card. The comps step: the table.
    source: str | None = None


class CardText(BaseModel):
    """What the language model writes. Its limits are checked after generation."""

    nickname: str = ""
    tagline: str = ""
    blurb: str = ""


class Comp(BaseModel):
    """One nearest-neighbour match from the Statcast table."""

    player: str
    season: int
    similarity: float = Field(ge=0.0, le=1.0)
    metrics: dict[str, float] = Field(default_factory=dict)


class CardJob(BaseModel):
    """The response body of GET /cards/{card_id}."""

    card_id: str
    status: JobStatus
    steps: list[Step] = Field(default_factory=list)
    text: CardText = Field(default_factory=CardText)
    comps: list[Comp] = Field(default_factory=list)
    # What the photo QA found. Never fails a card. The app prints these next
    # to the card so a person can see why one looks odd.
    warnings: list[str] = Field(default_factory=list)
    error: str | None = None
    # Which of front.png, back.png and preview.jpg can be fetched now. The
    # front is written before the back, so the app can show it early.
    assets: list[str] = Field(default_factory=list)


class ServiceHealth(BaseModel):
    """One model service, as it describes itself."""

    status: Literal["ok", "loading", "error", "unreachable"] = "unreachable"
    model: str | None = None
    backend: str | None = None
    device: str | None = None
    gpu: str | None = None
    loaded: bool = False
    vram_mb: int | None = None
    error: str | None = None
    # The label its answers carry in X-Ran-On: which card it is on. Every card
    # on the node is the same model, so `gpu` cannot tell GPU 0 from GPU 1.
    ran_on: str | None = None
    # The art service's pool: backgrounds ready now, and how many it keeps.
    # Null for the other services.
    pool_ready: int | None = None
    pool_size: int | None = None


class HealthReport(BaseModel):
    """GET /healthz. Needs the key. /livez is the open one."""

    ok: bool
    service: str
    version: str
    backend: str
    services: dict[str, ServiceHealth] = Field(default_factory=dict)
