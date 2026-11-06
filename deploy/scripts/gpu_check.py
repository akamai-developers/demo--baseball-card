# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""Which regions can sell the GPU plan right now, and what it costs an hour.

    make gpu-check                  the plan GPU_COUNT picks
    GPU_COUNT=2 make gpu-check      the two-card plan

It reads three routes of the Linode API, all of them read-only: /linode/types
for the plans and their prices, /regions for the regions that have GPUs at
all, and /regions/availability for which of those can sell a plan this minute.
Availability changes during the day. Earlier on 2026-11-06 no region could
sell an RTX 4000 Ada plan. At 22:28 UTC that day six could.

GPU_COUNT, LINODE_TYPE and LINODE_REGION come from the environment, else from
the repo's .env, as the deploy scripts read them. The three routes answer
without a token. With LINODE_TOKEN set the answer is for that account, and the
token goes in a header and nowhere else.

Exit status: 0 when LINODE_REGION can sell the plan now, 3 when it cannot, 2
when the settings name no plan this repo can deploy, 1 when the API did not
answer. make deploy runs this first and stops on anything but 0.

Standard library only, so `uv run` needs no environment for it.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# A test points LINODE_API_BASE at a stand-in. Nothing else changes it.
API = "https://api.linode.com/v4"
PAGE_SIZE = 500
TIMEOUT_S = 20

# The plan each GPU count gets. deploy/terraform/main.tf holds the same two,
# and tests/security/test_terraform.py holds them equal.
PLANS = {1: "g2-gpu-rtx4000a1-m", 2: "g2-gpu-rtx4000a2-s"}
# Every plan this repo can run on starts with this: RTX 4000 Ada, then the
# number of cards. docker-compose.yml has a layout for one card and for two.
FAMILY = "g2-gpu-rtx4000a"
DEFAULT_REGION = "us-ord"
# The capability /regions gives a region with any GPU plan.
GPU_CAPABILITY = "GPU Linodes"

SELLS, NO_ANSWER, BAD_SETTINGS, CANNOT = 0, 1, 2, 3


class Problem(Exception):
    """Something to say in one sentence, and the exit status that goes with it."""

    def __init__(self, message: str, status: int) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class Plan:
    id: str
    label: str
    gpus: int
    vcpus: int
    memory_mb: int
    hourly: float
    region_prices: dict[str, float]

    def price_in(self, region: str) -> float:
        """Some regions have their own price. These plans had none on 2026-11-06."""
        return self.region_prices.get(region, self.hourly)


@dataclass(frozen=True)
class Region:
    id: str
    label: str


# ---- reading the API ----------------------------------------------------------


def get_json(url: str, token: str) -> dict:
    """One GET. The error names the route and never the token."""
    route = url.split("?", 1)[0]
    request = urllib.request.Request(
        url, headers={"Accept": "application/json", "User-Agent": "baseball-card gpu-check"}
    )
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_S) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            raise Problem(
                "the Linode API refused LINODE_TOKEN. Check it, or unset it: "
                "these three routes answer without one.",
                NO_ANSWER,
            ) from None
        raise Problem(f"the Linode API answered {exc.code} for {route}.", NO_ANSWER) from None
    except (urllib.error.URLError, TimeoutError) as exc:
        reason = getattr(exc, "reason", exc)
        raise Problem(f"the Linode API did not answer {route}: {reason}.", NO_ANSWER) from None
    except ValueError:
        message = f"the Linode API sent something that is not JSON for {route}."
        raise Problem(message, NO_ANSWER) from None


def get_all(base: str, path: str, token: str) -> list[dict]:
    """Every row of a paginated route. /regions/availability is about 2,500 rows."""
    rows: list[dict] = []
    page = 1
    while True:
        body = get_json(f"{base}{path}?page_size={PAGE_SIZE}&page={page}", token)
        rows.extend(body.get("data") or [])
        if page >= int(body.get("pages") or 1):
            return rows
        page += 1


# ---- what the rows say --------------------------------------------------------


def plans_of(types: list[dict]) -> dict[str, Plan]:
    """The RTX 4000 Ada plans with one or two cards, the ones this repo can run on."""
    plans = {}
    for row in types:
        plan_id = row.get("id") or ""
        if not plan_id.startswith(FAMILY) or row.get("gpus") not in PLANS:
            continue
        plans[plan_id] = Plan(
            id=plan_id,
            label=row.get("label") or plan_id,
            gpus=row["gpus"],
            vcpus=row.get("vcpus") or 0,
            memory_mb=row.get("memory") or 0,
            hourly=float((row.get("price") or {}).get("hourly") or 0),
            region_prices={
                price["id"]: float(price["hourly"])
                for price in row.get("region_prices") or []
                if price.get("id") and price.get("hourly") is not None
            },
        )
    return plans


def gpu_regions(regions: list[dict]) -> dict[str, Region]:
    """Regions with any GPU plan. No other region can sell one, so no other is shown."""
    return {
        row["id"]: Region(id=row["id"], label=row.get("label") or row["id"])
        for row in regions
        if GPU_CAPABILITY in (row.get("capabilities") or [])
    }


def selling(availability: list[dict]) -> set[tuple[str, str]]:
    """(region, plan) for every row that says available. A missing row is a no."""
    return {(row["region"], row["plan"]) for row in availability if row.get("available") is True}


def choose(plans: dict[str, Plan], gpu_count: str, linode_type: str) -> Plan:
    """The plan the settings pick, or a sentence saying why there is none."""
    try:
        count = int(gpu_count or "1")
    except ValueError:
        count = 0
    if count not in PLANS:
        raise Problem(
            f"GPU_COUNT is {gpu_count!r}. It is 1 or 2: docker-compose.yml has a layout "
            "for one card and one for two.",
            BAD_SETTINGS,
        )
    plan_id = linode_type or PLANS[count]
    if plan_id not in plans:
        raise Problem(
            f"LINODE_TYPE is {plan_id}, which is not an RTX 4000 Ada plan with one or two "
            "cards in the Linode API's list. Leave it empty for the plan GPU_COUNT picks.",
            BAD_SETTINGS,
        )
    plan = plans[plan_id]
    if plan.gpus != count:
        raise Problem(
            f"LINODE_TYPE is {plan_id}, which has {plan.gpus} GPUs, and GPU_COUNT is {count}. "
            f"Set LINODE_TYPE to a plan with {count}, or empty for {PLANS[count]}.",
            BAD_SETTINGS,
        )
    return plan


# ---- the report ---------------------------------------------------------------


def table(header: tuple[str, ...], rows: list[tuple[str, ...]]) -> list[str]:
    widths = [max(len(cell) for cell in column) for column in zip(header, *rows, strict=True)]

    def line(cells: tuple[str, ...]) -> str:
        return "  ".join(cell.ljust(width) for cell, width in zip(cells, widths, strict=True))

    return [
        line(header).rstrip(),
        # A column with no heading is a marker, and gets no rule under it.
        line(tuple(("-" if h else " ") * w for h, w in zip(header, widths, strict=True))).rstrip(),
        *(line(r).rstrip() for r in rows),
    ]


def ram(plan: Plan) -> str:
    return f"{plan.memory_mb // 1024} GB"


def names(ids: list[str]) -> str:
    return ", ".join(ids) if ids else "none"


def report(
    types: list[dict],
    regions: list[dict],
    availability: list[dict],
    *,
    gpu_count: str,
    linode_type: str,
    region: str,
) -> tuple[str, int]:
    """The two tables and the verdict for `region`, and the exit status."""
    plans = plans_of(types)
    plan = choose(plans, gpu_count, linode_type)
    places = gpu_regions(regions)
    sells = selling(availability)

    def can(plan_id: str) -> list[str]:
        return sorted(r for r in places if (r, plan_id) in sells)

    yes = can(plan.id)
    order = sorted(places.values(), key=lambda r: (r.id not in yes, r.id))
    rows = [
        (
            r.id,
            r.label,
            "yes" if r.id in yes else "no",
            f"{plan.price_in(r.id):.2f}",
            "<- LINODE_REGION" if r.id == region else "",
        )
        for r in order
    ]
    picked_by = "LINODE_TYPE" if linode_type else f"GPU_COUNT={plan.gpus}"
    out = [
        f"{picked_by} deploys {plan.id}, {plan.label}: {plan.gpus} GPU"
        f"{'s' if plan.gpus > 1 else ''}, {plan.vcpus} vCPUs, {ram(plan)} of RAM.",
        "",
        *table(("region", "where", "sells it now", "USD an hour", ""), rows),
        "",
        "The other RTX 4000 Ada plans this repo can run on. LINODE_TYPE in .env picks one,",
        "with GPU_COUNT set to its number of cards.",
        "",
    ]
    others = sorted(
        (p for p in plans.values() if p.id != plan.id), key=lambda p: (p.gpus, p.hourly, p.id)
    )
    out += table(
        ("plan", "GPU_COUNT", "vCPUs", "RAM", "USD an hour", "sells it now in"),
        [
            (p.id, str(p.gpus), str(p.vcpus), ram(p), f"{p.hourly:.2f}", names(can(p.id)))
            for p in others
        ],
    )
    out.append("")

    if region in yes:
        out.append(f"{region} can sell {plan.id} now. {len(yes)} of {len(places)} GPU regions can.")
        return "\n".join(out) + "\n", SELLS

    if region not in places:
        out.append(f"LINODE_REGION is {region}, which has no GPU plans at all.")
    else:
        out.append(f"{region} cannot sell {plan.id} now.")
    if yes:
        out.append(f"These can: {names(yes)}. Set LINODE_REGION in .env to one of them.")
    else:
        out.append(f"No region can sell {plan.id} now.")
        other_count = 3 - plan.gpus
        other_yes = can(PLANS[other_count])
        if other_yes:
            out.append(
                f"GPU_COUNT={other_count} picks {PLANS[other_count]}, which "
                f"{names(other_yes)} can sell."
            )
        out.append("Capacity changes during the day. Run make gpu-check again later.")
    return "\n".join(out) + "\n", CANNOT


# ---- the settings -------------------------------------------------------------


def dotenv(path: Path) -> dict[str, str]:
    """KEY=value lines of a .env file. Only the three settings below are read out of it."""
    values = {}
    if path.is_file():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                name, _, value = line.partition("=")
                values[name.strip()] = value.strip()
    return values


def main() -> int:
    from_file = dotenv(REPO_ROOT / ".env")

    def setting(name: str, default: str = "") -> str:
        return os.environ.get(name, "").strip() or from_file.get(name, "") or default

    base = os.environ.get("LINODE_API_BASE", API).rstrip("/")
    token = os.environ.get("LINODE_TOKEN", "").strip()
    try:
        text, status = report(
            get_all(base, "/linode/types", token),
            get_all(base, "/regions", token),
            get_all(base, "/regions/availability", token),
            gpu_count=setting("GPU_COUNT", "1"),
            linode_type=setting("LINODE_TYPE"),
            region=setting("LINODE_REGION", DEFAULT_REGION),
        )
    except Problem as problem:
        print(f"gpu-check: {problem}", file=sys.stderr)
        return problem.status
    sys.stdout.write(text)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
