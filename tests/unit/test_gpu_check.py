"""deploy/scripts/gpu_check.py, against the Linode API's own answers.

tests/fixtures/linode/ holds what three read-only routes answered on
2026-11-06, recorded without a token: the plans, the regions, and which region
could sell which plan. SOURCES.md says how they were trimmed. Nothing here
calls the live API. The report is tested from those rows, and the script end
to end through a stand-in API on this machine that serves them in pages.
"""

from __future__ import annotations

import copy
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

from tests.conftest import REPO_ROOT, load_script

SCRIPT = REPO_ROOT / "deploy" / "scripts" / "gpu_check.py"
RECORDED = REPO_ROOT / "tests" / "fixtures" / "linode"
gpu_check = load_script(SCRIPT)

# What the recorded answer says: these six could sell every one- and two-card
# RTX 4000 Ada plan, and 22 regions have GPUs at all.
SELLING = ["de-fra-2", "fr-par", "in-bom-2", "jp-osa", "sg-sin-2", "us-ord"]
GPU_REGIONS = 22
TOKEN = "made-up-token-for-the-stand-in"


def recorded(name: str) -> list[dict]:
    return json.loads((RECORDED / f"{name}.json").read_text())["data"]


def report(availability: list[dict] | None = None, types: list[dict] | None = None, **settings):
    values = {"gpu_count": "1", "linode_type": "", "region": "us-ord", **settings}
    return gpu_check.report(
        types if types is not None else recorded("types"),
        recorded("regions"),
        availability if availability is not None else recorded("availability"),
        **values,
    )


def row_of(text: str, first: str) -> list[str]:
    """The cells of the table row that starts with `first`."""
    for line in text.splitlines():
        cells = re.split(r"\s{2,}", line.strip())
        if cells[0] == first:
            return cells
    raise AssertionError(f"no row for {first}")


def without(plan: str, rows: list[dict]) -> list[dict]:
    """The recorded availability with `plan` sold out everywhere."""
    rows = copy.deepcopy(rows)
    for row in rows:
        if row["plan"] == plan:
            row["available"] = False
    return rows


# ---- the recording ------------------------------------------------------------


def test_the_recording_is_the_shape_the_api_answers_in() -> None:
    """{region, plan, available} per row, as /regions/availability answered."""
    rows = recorded("availability")
    assert rows and all(set(row) == {"region", "plan", "available"} for row in rows)
    assert {
        r["region"] for r in rows if r["available"] and r["plan"] == "g2-gpu-rtx4000a1-m"
    } == set(SELLING)


# ---- the report ---------------------------------------------------------------


def test_one_gpu_is_the_one_card_plan_and_us_ord_can_sell_it() -> None:
    text, status = report()
    assert status == gpu_check.SELLS
    assert text.startswith(
        "GPU_COUNT=1 deploys g2-gpu-rtx4000a1-m, RTX4000 Ada x1 Medium: 1 GPU, 8 vCPUs, "
        "32 GB of RAM."
    )
    assert row_of(text, "us-ord") == ["us-ord", "Chicago, IL", "yes", "0.67", "<- LINODE_REGION"]
    assert text.rstrip().endswith(
        "us-ord can sell g2-gpu-rtx4000a1-m now. 6 of 22 GPU regions can."
    )


def test_every_gpu_region_is_listed_and_those_that_can_sell_come_first() -> None:
    text, _ = report()
    table = text.split("\n\n")[1].splitlines()[2:]
    regions = [line.split()[0] for line in table]
    assert len(regions) == GPU_REGIONS
    assert regions[: len(SELLING)] == SELLING
    assert all(line.split()[-2] == "no" for line in table[len(SELLING) :])
    # No region without GPUs. us-central has none.
    assert "us-central" not in regions


def test_two_gpus_is_the_two_card_plan() -> None:
    text, status = report(gpu_count="2")
    assert status == gpu_check.SELLS
    assert text.startswith("GPU_COUNT=2 deploys g2-gpu-rtx4000a2-s, RTX4000 Ada x2 Small: 2 GPUs,")
    assert row_of(text, "us-ord")[3] == "1.05"


def test_the_other_plans_are_the_one_and_two_card_ones_with_their_prices() -> None:
    """The 16 GB plan is there for a developer who wants to try it. Four-card
    plans and other GPUs are not: this repo has no layout for them."""
    text, _ = report()
    others = text.split("\n\n")[3].splitlines()[2:]
    listed = [line.split()[0] for line in others]
    assert listed == [
        "g2-gpu-rtx4000a1-s",
        "g2-gpu-rtx4000a1-l",
        "g2-gpu-rtx4000a1-xl",
        "g2-gpu-rtx4000a2-s",
        "g2-gpu-rtx4000a2-m",
        "g2-gpu-rtx4000a2-hs",
    ]
    assert row_of(text, "g2-gpu-rtx4000a1-s")[:5] == [
        "g2-gpu-rtx4000a1-s",
        "1",
        "4",
        "16 GB",
        "0.52",
    ]
    assert row_of(text, "g2-gpu-rtx4000a2-s")[-1] == ", ".join(SELLING)


def test_linode_type_picks_another_plan_with_the_same_cards() -> None:
    text, status = report(linode_type="g2-gpu-rtx4000a1-s")
    assert status == gpu_check.SELLS
    assert text.startswith("LINODE_TYPE deploys g2-gpu-rtx4000a1-s, RTX4000 Ada x1 Small")
    assert row_of(text, "us-ord")[3] == "0.52"


def test_a_region_that_cannot_sell_the_plan_names_those_that_can() -> None:
    text, status = report(region="us-sea")
    assert status == gpu_check.CANNOT
    assert row_of(text, "us-sea") == ["us-sea", "Seattle, WA", "no", "0.67", "<- LINODE_REGION"]
    assert "us-sea cannot sell g2-gpu-rtx4000a1-m now." in text
    assert f"These can: {', '.join(SELLING)}. Set LINODE_REGION in .env to one of them." in text


def test_a_region_without_gpus_says_so() -> None:
    text, status = report(region="us-central")
    assert status == gpu_check.CANNOT
    assert "LINODE_REGION is us-central, which has no GPU plans at all." in text


def test_no_region_at_all_says_to_try_later() -> None:
    """What the API said earlier on 2026-11-06, for every RTX 4000 Ada plan."""
    rows = [{**row, "available": False} for row in recorded("availability")]
    text, status = report(availability=rows)
    assert status == gpu_check.CANNOT
    assert "No region can sell g2-gpu-rtx4000a1-m now." in text
    assert "Capacity changes during the day. Run make gpu-check again later." in text
    assert "GPU_COUNT=2 picks" not in text


def test_when_only_the_other_count_sells_it_says_which_and_where() -> None:
    text, status = report(availability=without("g2-gpu-rtx4000a1-m", recorded("availability")))
    assert status == gpu_check.CANNOT
    assert f"GPU_COUNT=2 picks g2-gpu-rtx4000a2-s, which {', '.join(SELLING)} can sell." in text


def test_a_missing_row_counts_as_cannot_sell() -> None:
    rows = [r for r in recorded("availability") if r["region"] != "us-ord"]
    _, status = report(availability=rows)
    assert status == gpu_check.CANNOT


def test_a_region_with_its_own_price_shows_that_price() -> None:
    types = copy.deepcopy(recorded("types"))
    plan = next(t for t in types if t["id"] == "g2-gpu-rtx4000a1-m")
    plan["region_prices"] = [{"id": "jp-osa", "hourly": 0.81, "monthly": None}]
    text, _ = report(types=types)
    assert row_of(text, "jp-osa")[3] == "0.81"
    assert row_of(text, "us-ord")[3] == "0.67"


@pytest.mark.parametrize(
    ("settings", "says"),
    [
        ({"gpu_count": "3"}, "GPU_COUNT is '3'. It is 1 or 2"),
        ({"gpu_count": "two"}, "GPU_COUNT is 'two'. It is 1 or 2"),
        (
            {"linode_type": "g6-standard-2"},
            "LINODE_TYPE is g6-standard-2, which is not an RTX 4000",
        ),
        ({"linode_type": "g2-gpu-rtx4000a4-s"}, "LINODE_TYPE is g2-gpu-rtx4000a4-s, which is not"),
        (
            {"linode_type": "g2-gpu-rtx4000a2-s"},
            "LINODE_TYPE is g2-gpu-rtx4000a2-s, which has 2 GPUs, and GPU_COUNT is 1. "
            "Set LINODE_TYPE to a plan with 1, or empty for g2-gpu-rtx4000a1-m.",
        ),
    ],
)
def test_settings_that_pick_no_usable_plan_are_refused_in_a_sentence(settings, says) -> None:
    with pytest.raises(gpu_check.Problem) as refused:
        report(**settings)
    assert says in str(refused.value)
    assert refused.value.status == gpu_check.BAD_SETTINGS


def test_the_two_plans_are_the_ones_the_docs_price() -> None:
    """README.md and .env.example name both plans and their prices. The
    recorded price list agrees, and so does gpu_check's map."""
    prices = {t["id"]: t["price"]["hourly"] for t in recorded("types")}
    assert gpu_check.PLANS == {1: "g2-gpu-rtx4000a1-m", 2: "g2-gpu-rtx4000a2-s"}
    assert prices["g2-gpu-rtx4000a1-m"] == 0.67
    assert prices["g2-gpu-rtx4000a2-s"] == 1.05
    readme = (REPO_ROOT / "README.md").read_text()
    for plan, price in (("g2-gpu-rtx4000a1-m", "0.67"), ("g2-gpu-rtx4000a2-s", "1.05")):
        assert re.search(rf"`{plan}`.*\| {re.escape(price)} \|", readme), plan


# ---- end to end, through a stand-in API -------------------------------------


class StandIn:
    """The three routes, served from the recording in pages, on this machine.
    It writes down every request: the path and query, and the Authorization
    header."""

    def __init__(self, status: int = 200) -> None:
        self.status = status
        self.requests: list[tuple[str, str | None]] = []
        routes = {
            "/v4/linode/types": recorded("types"),
            "/v4/regions": recorded("regions"),
            "/v4/regions/availability": recorded("availability"),
        }
        stand_in = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802, the name http.server calls
                stand_in.requests.append((self.path, self.headers.get("Authorization")))
                url = urlparse(self.path)
                query = parse_qs(url.query)
                rows = routes.get(url.path)
                if rows is None or stand_in.status != 200:
                    self.send_response(404 if rows is None else stand_in.status)
                    self.end_headers()
                    return
                size = int(query["page_size"][0])
                page = int(query["page"][0])
                # The real availability route is 2,500 rows over five pages. The
                # recording is 363, so pages of 100 make four.
                size = min(size, 100)
                pages = max(1, -(-len(rows) // size))
                body = {
                    "data": rows[(page - 1) * size : page * size],
                    "page": page,
                    "pages": pages,
                    "results": len(rows),
                }
                data = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}/v4"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def stand_in():
    api = StandIn()
    yield api
    api.close()


def run(tmp_path: Path, base: str, dotenv: str = "", **env: str) -> subprocess.CompletedProcess:
    """The script from a copy of its folder, so this box's .env is not read, and
    with only the environment given here."""
    scripts = tmp_path / "deploy" / "scripts"
    scripts.mkdir(parents=True)
    shutil.copy(SCRIPT, scripts / SCRIPT.name)
    (tmp_path / ".env").write_text(dotenv)
    return subprocess.run(
        [sys.executable, str(scripts / SCRIPT.name)],
        env={"PATH": os.environ["PATH"], "LINODE_API_BASE": base, **env},
        capture_output=True,
        text=True,
        timeout=60,
    )


def test_the_script_reads_every_page_and_says_us_ord_can_sell(tmp_path, stand_in) -> None:
    done = run(tmp_path, stand_in.base, LINODE_TOKEN=TOKEN)
    assert done.returncode == 0, done.stderr
    assert "us-ord can sell g2-gpu-rtx4000a1-m now." in done.stdout
    pages = [path for path, _ in stand_in.requests if path.startswith("/v4/regions/availability")]
    assert len(pages) == 4
    assert pages[-1].endswith("page=4")


def test_the_token_goes_in_a_header_and_nowhere_else(tmp_path, stand_in) -> None:
    done = run(tmp_path, stand_in.base, LINODE_TOKEN=TOKEN)
    assert done.returncode == 0, done.stderr
    assert {auth for _, auth in stand_in.requests} == {f"Bearer {TOKEN}"}
    assert not [path for path, _ in stand_in.requests if TOKEN in path]
    assert TOKEN not in done.stdout + done.stderr


def test_without_a_token_it_asks_without_one(tmp_path, stand_in) -> None:
    """The three routes are public. A developer can look before having a token."""
    done = run(tmp_path, stand_in.base)
    assert done.returncode == 0, done.stderr
    assert {auth for _, auth in stand_in.requests} == {None}


def test_the_settings_come_from_dotenv_when_the_environment_has_none(tmp_path, stand_in) -> None:
    done = run(tmp_path, stand_in.base, dotenv="GPU_COUNT=2\nLINODE_REGION=us-sea\n")
    assert done.returncode == gpu_check.CANNOT
    assert "us-sea cannot sell g2-gpu-rtx4000a2-s now." in done.stdout


def test_the_environment_wins_over_dotenv(tmp_path, stand_in) -> None:
    """As in the deploy scripts: GPU_COUNT=2 make gpu-check, for one run."""
    done = run(tmp_path, stand_in.base, dotenv="GPU_COUNT=1\n", GPU_COUNT="2")
    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("GPU_COUNT=2 deploys g2-gpu-rtx4000a2-s")


def test_a_refused_token_is_one_sentence_and_exit_1(tmp_path) -> None:
    api = StandIn(status=401)
    try:
        done = run(tmp_path, api.base, LINODE_TOKEN=TOKEN)
    finally:
        api.close()
    assert done.returncode == gpu_check.NO_ANSWER
    assert done.stderr.startswith("gpu-check: the Linode API refused LINODE_TOKEN.")
    assert TOKEN not in done.stdout + done.stderr
    assert "Traceback" not in done.stderr


def test_no_api_at_all_is_one_sentence_and_exit_1(tmp_path) -> None:
    api = StandIn()
    base = api.base
    api.close()
    done = run(tmp_path, base)
    assert done.returncode == gpu_check.NO_ANSWER
    assert done.stderr.startswith("gpu-check: the Linode API did not answer")
    assert "Traceback" not in done.stderr


def test_bad_settings_are_exit_2_before_anything_is_printed(tmp_path, stand_in) -> None:
    done = run(tmp_path, stand_in.base, GPU_COUNT="4")
    assert done.returncode == gpu_check.BAD_SETTINGS
    assert done.stdout == ""
    assert "GPU_COUNT is '4'" in done.stderr


# ---- how it is run ------------------------------------------------------------


def test_uv_runs_it_with_no_dependencies_and_make_runs_it_with_uv() -> None:
    """Inline metadata with no dependencies, so `uv run` makes no environment
    for it and never syncs the workspace."""
    header = re.match(r"# /// script\n(.*?)# ///\n", SCRIPT.read_text(), re.S)
    assert header, "gpu_check.py has no inline script metadata"
    assert "# dependencies = []" in header.group(1)
    makefile = (REPO_ROOT / "Makefile").read_text()
    assert "\tuv run --quiet deploy/scripts/gpu_check.py\n" in makefile
    deploy = (REPO_ROOT / "deploy" / "scripts" / "deploy.sh").read_text()
    assert 'uv run --quiet "$SCRIPT_DIR/gpu_check.py"' in deploy
