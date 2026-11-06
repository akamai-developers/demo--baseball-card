"""cardctl: a photo and a stats file in, two PNGs out.

    uv run cardctl make --photo me.jpg --stats stats.json --out out/
    uv run cardctl make --photo me.jpg --stats stats.json --style classic

It posts to POST /cards, polls GET /cards/{id} until the job finishes, writes
card-front.png, card-back.png and card-preview.jpg, and prints the ledger:
which model ran each step, under which licence, on which GPU, and for how many
milliseconds.

The same job goes into card.json next to the PNGs, so a script can read where
each step ran instead of parsing the table meant for a person.

The key is read from the environment, or from a .env file in this directory or
one above it. There is no --key flag. A key on a command line ends up in the
shell history and in the process list, where anyone on the box can read it.
The gateway is http://localhost:GATEWAY_PORT, read the same way, 8443 when
neither says. --host points it anywhere else.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx

DEFAULT_PORT = "8443"
DEFAULT_KEY_ENV = "CARD_API_KEY"
# What the api serves, and what it is called on disk. The local names match
# `make card-fixture`, so out/ looks the same however the card was made.
ASSETS = {
    "front.png": "card-front.png",
    "back.png": "card-back.png",
    "preview.jpg": "card-preview.jpg",
}
# The job as the api reported it: steps, text, comps. Written next to the PNGs
# so a script can check where each step ran without reading the printed table.
LEDGER = "card.json"
POLL_S = 1.0
# What a status means when the answer carries no sentence of its own: the
# gateway's, when the api behind it is down or a wrong key was sent too often.
DOWN = (
    "The gateway answered, but the api behind it did not: make logs, or make gpu-logs on the node."
)
SAID = {
    429: "Too many wrong keys from this address. Wait a minute, then try again.",
    502: DOWN,
    503: DOWN,
    504: DOWN,
}


class Problem(Exception):
    """Something the user can fix. Printed without a traceback."""


# -- input -------------------------------------------------------------------


def find_setting(name: str) -> str | None:
    """A setting from the environment, or from the nearest .env. None when neither has it."""
    from_env = os.environ.get(name, "").strip()
    if from_env:
        return from_env

    for directory in (Path.cwd(), *Path.cwd().parents):
        candidate = directory / ".env"
        if not candidate.is_file():
            continue
        for line in candidate.read_text().splitlines():
            stripped = line.strip()
            if stripped.startswith(f"{name}="):
                value = stripped.split("=", 1)[1].strip()
                if value:
                    return value
    return None


def find_key(env_name: str) -> str:
    """The key, from the environment or from the nearest .env. Never from a flag."""
    key = find_setting(env_name)
    if key:
        return key
    raise Problem(f"no {env_name}. Run make env, or run this from a directory with a .env in it.")


def default_host() -> str:
    """The mock stack or the tunnel on this machine: localhost on GATEWAY_PORT."""
    return f"http://localhost:{find_setting('GATEWAY_PORT') or DEFAULT_PORT}"


def read_stats(path: Path) -> str:
    """The stats file, checked for valid JSON here so a typo fails before the upload."""
    if not path.is_file():
        raise Problem(f"no stats file at {path}")
    raw = path.read_text()
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise Problem(f"{path} is not valid JSON: {exc.msg} at line {exc.lineno}") from exc
    if not isinstance(parsed, dict) or not parsed.get("name"):
        raise Problem(f"{path} needs at least a name field")
    return raw


def read_photo(path: Path) -> bytes:
    if not path.is_file():
        raise Problem(f"no photo at {path}")
    return path.read_bytes()


# -- output ------------------------------------------------------------------


def model_cell(step: dict) -> str:
    """The model, and for the art step whether the background came from the pool."""
    model = step.get("model") or "-"
    source = step.get("source")
    return f"{model} · {source}" if source else model


def step_table(steps: list[dict]) -> str:
    """The ledger, as it will appear on the back of the card."""
    header = ("step", "model", "licence", "ran on", "ms", "status")
    rows = [
        (
            step.get("step", ""),
            model_cell(step),
            step.get("license") or "-",
            step.get("ran_on") or "-",
            str(step.get("ms") or "-"),
            step.get("status", ""),
        )
        for step in steps
    ]
    widths = [max(len(row[i]) for row in (header, *rows)) for i in range(len(header))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(header)).rstrip()]
    lines.append("  ".join("-" * widths[i] for i in range(len(header))).rstrip())
    for row in rows:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)).rstrip())
    return "\n".join(lines)


def explain(exc: Exception, host: str) -> str:
    """Turn an httpx failure into something with a next step in it."""
    if isinstance(exc, httpx.ConnectError):
        return (
            f"nothing is listening at {host}.\n"
            "  On a laptop:   make up-mock\n"
            "  On the node:   make tunnel, and leave it running in another terminal"
        )
    if isinstance(exc, httpx.ConnectTimeout):
        return f"{host} did not answer in time. Is the tunnel still open?"
    if isinstance(exc, httpx.ReadTimeout):
        return f"{host} accepted the request and then went quiet. Try: make logs"
    return f"{type(exc).__name__}: {exc}"


def check(response: httpx.Response, what: str) -> None:
    if response.status_code == 401:
        raise Problem(
            f"the api refused the key ({what}).\n"
            "  The key in your environment is not the key the stack was started with.\n"
            "  Check .env, and that CARD_API_KEY is not set to an old key in this shell."
        )
    if response.status_code >= 400:
        try:
            body = response.json()
        except ValueError:
            body = None
        detail = body.get("detail") if isinstance(body, dict) else None
        if not isinstance(detail, str) or not detail:
            detail = SAID.get(response.status_code, response.text[:200])
        raise Problem(f"{what} failed: HTTP {response.status_code}. {detail}")


# -- the command -------------------------------------------------------------


def make_card(args: argparse.Namespace) -> int:
    host = (args.host or default_host()).rstrip("/")
    key = find_key(args.key_env)
    stats = read_stats(args.stats)
    photo = read_photo(args.photo)
    headers = {"Authorization": f"Bearer {key}"}
    out_dir: Path = args.out
    out_dir.mkdir(parents=True, exist_ok=True)

    began = time.perf_counter()
    with httpx.Client(timeout=httpx.Timeout(60.0, connect=10.0)) as client:
        try:
            created = client.post(
                f"{host}/cards",
                headers=headers,
                files={"photo": (args.photo.name, photo, "application/octet-stream")},
                data={"stats": stats, **({"style": args.style} if args.style else {})},
            )
        except httpx.HTTPError as exc:
            raise Problem(explain(exc, host)) from exc
        check(created, "POST /cards")

        card_id = created.json()["card_id"]
        print(f"card {card_id}")

        job = wait_for(client, host, headers, card_id, args.timeout)
        write_assets(client, host, headers, card_id, out_dir)

    (out_dir / LEDGER).write_text(json.dumps(job, indent=2) + "\n")

    print()
    print(step_table(job.get("steps", [])))
    for warning in job.get("warnings", []):
        print(f"warning: {warning}")
    print()
    for name in (*ASSETS.values(), LEDGER):
        print(out_dir / name)
    print(f"\n{time.perf_counter() - began:.1f}s total")
    return 0


def wait_for(client: httpx.Client, host: str, headers: dict, card_id: str, timeout: float) -> dict:
    """Poll until the job is done or failed. Prints each step as it lands."""
    deadline = time.monotonic() + timeout
    reported: set[str] = set()
    while True:
        try:
            response = client.get(f"{host}/cards/{card_id}", headers=headers)
        except httpx.HTTPError as exc:
            raise Problem(explain(exc, host)) from exc
        check(response, f"GET /cards/{card_id}")
        job = response.json()

        for step in job.get("steps", []):
            name, state = step.get("step"), step.get("status")
            if state in ("done", "failed", "skipped") and name not in reported:
                reported.add(name)
                note = f"  {step['error']}" if step.get("error") else ""
                print(f"  {state:<9} {name:<8} {step.get('ms') or '-':>6} ms{note}")

        if job["status"] == "done":
            return job
        if job["status"] == "failed":
            raise Problem(f"the card failed: {job.get('error') or 'no reason given'}")
        if time.monotonic() > deadline:
            raise Problem(
                f"the card was still {job['status']} after {timeout:.0f}s. "
                f"It may finish later: GET {host}/cards/{card_id}"
            )
        time.sleep(POLL_S)


def write_assets(
    client: httpx.Client, host: str, headers: dict, card_id: str, out_dir: Path
) -> None:
    """Download the three files the api made."""
    for served, local in ASSETS.items():
        response = client.get(f"{host}/cards/{card_id}/{served}", headers=headers)
        check(response, f"GET {served}")
        (out_dir / local).write_bytes(response.content)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cardctl", description="Make a baseball card from a photo and a stats file."
    )
    subcommands = parser.add_subparsers(dest="command", required=True)

    make = subcommands.add_parser("make", help="make one card")
    make.add_argument("--photo", type=Path, required=True, help="a headshot against a plain wall")
    make.add_argument("--stats", type=Path, required=True, help="the stats.json to use")
    make.add_argument("--out", type=Path, default=Path("out"), help="where to write the PNGs")
    make.add_argument("--style", default=None, help="akamai or classic. default akamai")
    make.add_argument(
        "--host", default=None, help="the gateway. default http://localhost:GATEWAY_PORT"
    )
    make.add_argument(
        "--key-env",
        default=DEFAULT_KEY_ENV,
        help=f"environment variable holding the key. default {DEFAULT_KEY_ENV}",
    )
    make.add_argument("--timeout", type=float, default=600.0, help="seconds to wait for the card")
    make.set_defaults(handler=make_card)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.handler(args)
    except Problem as problem:
        print(f"cardctl: {problem}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\ncardctl: stopped", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
