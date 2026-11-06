"""cardctl, without a stack behind it.

The interesting part of a CLI is what it says when something is wrong. A key
that does not match, a tunnel that is not open, and a stats file with a typo in
it are the three things that actually happen.
"""

from __future__ import annotations

import json

import httpx
import pytest

from cardctl.main import (
    ASSETS,
    LEDGER,
    Problem,
    build_parser,
    check,
    default_host,
    explain,
    find_key,
    read_stats,
    step_table,
)
from common.schemas import Stats


def test_the_key_comes_from_the_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CARD_API_KEY", "k" * 64)
    assert find_key("CARD_API_KEY") == "k" * 64


def test_the_key_can_come_from_a_dotenv_beside_the_caller(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("CARD_API_KEY", raising=False)
    (tmp_path / ".env").write_text("BACKEND=mock\nCARD_API_KEY=abc123\n")
    monkeypatch.chdir(tmp_path)
    assert find_key("CARD_API_KEY") == "abc123"


def test_the_key_can_come_from_a_dotenv_above_the_caller(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("CARD_API_KEY", raising=False)
    (tmp_path / ".env").write_text("CARD_API_KEY=abc123\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    assert find_key("CARD_API_KEY") == "abc123"


def test_the_environment_wins_over_the_file(monkeypatch, tmp_path) -> None:
    (tmp_path / ".env").write_text("CARD_API_KEY=from-the-file\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("CARD_API_KEY", "from-the-environment")
    assert find_key("CARD_API_KEY") == "from-the-environment"


def test_no_key_anywhere_says_how_to_set_one(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("CARD_API_KEY", raising=False)
    monkeypatch.chdir(tmp_path)
    with pytest.raises(Problem, match="Run make env"):
        find_key("CARD_API_KEY")


def test_there_is_no_key_flag() -> None:
    """A key on a command line ends up in the shell history and the process list."""
    options = {
        action.option_strings[0]
        for action in build_parser()._subparsers._actions
        if action.option_strings
    }
    assert "--key" not in options


def test_the_parser_takes_a_key_env_not_a_key() -> None:
    args = build_parser().parse_args(["make", "--photo", "p.jpg", "--stats", "s.json"])
    assert args.key_env == "CARD_API_KEY"
    assert args.host is None


def test_the_gateway_is_localhost_on_8443_unless_gateway_port_says(monkeypatch, tmp_path) -> None:
    """The mock stack and the tunnel both listen on localhost:GATEWAY_PORT."""
    monkeypatch.delenv("GATEWAY_PORT", raising=False)
    monkeypatch.chdir(tmp_path)
    assert default_host() == "http://localhost:8443"
    (tmp_path / ".env").write_text("GATEWAY_PORT=8446\n")
    assert default_host() == "http://localhost:8446"
    monkeypatch.setenv("GATEWAY_PORT", "8447")
    assert default_host() == "http://localhost:8447"


def test_a_missing_stats_file_is_reported_before_the_upload(tmp_path) -> None:
    with pytest.raises(Problem, match="no stats file"):
        read_stats(tmp_path / "nope.json")


def test_a_stats_file_that_is_not_json_says_which_line(tmp_path) -> None:
    path = tmp_path / "stats.json"
    path.write_text('{\n  "name": "Sam",\n  oops\n}')
    with pytest.raises(Problem, match="line 3"):
        read_stats(path)


def test_a_stats_file_with_no_name_is_refused_locally(tmp_path) -> None:
    path = tmp_path / "stats.json"
    path.write_text('{"number": 23}')
    with pytest.raises(Problem, match="needs at least a name"):
        read_stats(path)


def test_the_repo_example_is_accepted() -> None:
    from tests.conftest import REPO_ROOT

    raw = read_stats(REPO_ROOT / "examples" / "stats.json")
    stats = Stats.model_validate_json(raw)
    assert stats.name == "Sam Rivera"
    assert len(stats.metrics) == 6


def test_a_401_tells_you_which_key_to_check() -> None:
    with pytest.raises(Problem, match="refused the key"):
        check(httpx.Response(401), "POST /cards")


def test_a_500_carries_the_detail_through() -> None:
    with pytest.raises(Problem, match="the photo is empty"):
        check(httpx.Response(400, json={"detail": "the photo is empty"}), "POST /cards")


def test_a_200_is_quiet() -> None:
    check(httpx.Response(200), "POST /cards")


def test_a_refused_connection_names_the_tunnel() -> None:
    request = httpx.Request("GET", "http://localhost:8443/livez")
    message = explain(httpx.ConnectError("refused", request=request), "http://localhost:8443")
    assert "make up-mock" in message
    assert "make tunnel" in message


def test_the_downloaded_names_match_make_card_fixture() -> None:
    """out/ has to look the same however the card was made."""
    assert ASSETS["front.png"] == "card-front.png"
    assert ASSETS["back.png"] == "card-back.png"


# -- make, against a stand-in api ---------------------------------------------

JOB = {
    "card_id": "abc",
    "status": "done",
    "steps": [{"step": "cutout", "status": "done", "model": "mock/plain-wall-matte", "ms": 231}],
    "warnings": ["no face found"],
}


def run_make(monkeypatch, tmp_path, *flags: str) -> dict:
    """cardctl make against a stand-in api. Returns what it posted and asked for."""
    from cardctl import main as cardctl_main

    posted: dict = {"asked": []}
    job = JOB

    def answer(request: httpx.Request) -> httpx.Response:
        posted["asked"].append(str(request.url))
        if request.method == "POST":
            posted["body"] = request.content
            return httpx.Response(202, json={"card_id": "abc", "status": "queued"})
        if request.url.path == "/cards/abc":
            return httpx.Response(200, json=job)
        return httpx.Response(200, content=b"png")

    real = httpx.Client
    monkeypatch.setattr(
        cardctl_main.httpx,
        "Client",
        lambda **kwargs: real(transport=httpx.MockTransport(answer), **kwargs),
    )
    monkeypatch.setenv("CARD_API_KEY", "k" * 64)
    photo = tmp_path / "me.jpg"
    photo.write_bytes(b"jpeg bytes")
    stats = tmp_path / "stats.json"
    stats.write_text(json.dumps({"name": "Sam Rivera"}))
    code = cardctl_main.main(
        ["make", "--photo", str(photo), "--stats", str(stats), "--out", str(tmp_path), *flags]
    )
    assert code == 0
    return posted


def test_style_is_sent_as_its_own_field(monkeypatch, tmp_path) -> None:
    body = run_make(monkeypatch, tmp_path, "--style", "classic")["body"]
    assert b'name="style"\r\n\r\nclassic\r\n' in body


def test_without_a_style_the_api_picks_its_own(monkeypatch, tmp_path) -> None:
    body = run_make(monkeypatch, tmp_path)["body"]
    assert b'name="style"' not in body


def test_host_points_it_anywhere(monkeypatch, tmp_path) -> None:
    asked = run_make(monkeypatch, tmp_path, "--host", "http://node.example:9999/")["asked"]
    assert asked[0] == "http://node.example:9999/cards"


def test_it_writes_the_three_files_and_the_job(monkeypatch, tmp_path, capsys) -> None:
    run_make(monkeypatch, tmp_path)
    for name in ASSETS.values():
        assert (tmp_path / name).read_bytes() == b"png"
    assert json.loads((tmp_path / LEDGER).read_text()) == JOB
    out = capsys.readouterr().out
    assert "mock/plain-wall-matte" in out
    assert "warning: no face found" in out


def test_the_table_says_whether_the_background_came_from_the_pool() -> None:
    """Next to the model, as on the back of the card and in the app."""
    steps = [
        {"step": "art", "model": "black-forest-labs/FLUX.2-klein-4B", "source": "pool"},
        {"step": "cutout", "model": "ZhengPeng7/BiRefNet", "source": None},
    ]
    rows = step_table(steps).splitlines()[2:]
    assert "black-forest-labs/FLUX.2-klein-4B · pool" in rows[0]
    assert "·" not in rows[1]


# -- a card that ends without a picture --------------------------------------


def test_a_card_that_fails_says_why(capsys) -> None:
    from cardctl.main import wait_for

    error = "the api restarted before this card was finished"
    job = {
        "card_id": "abc",
        "status": "failed",
        "error": error,
        "steps": [{"step": "cutout", "status": "failed", "ms": None, "error": error}],
    }
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=job))
    with httpx.Client(transport=transport) as client, pytest.raises(Problem) as problem:
        wait_for(client, "http://node", {}, "abc", timeout=5)
    assert str(problem.value) == f"the card failed: {error}"
    assert "  failed    cutout " in capsys.readouterr().out


def test_a_deleted_card_says_the_api_does_not_have_it(capsys) -> None:
    """Someone deleted the card in the app while cardctl waited for it."""
    from cardctl.main import wait_for

    gone = httpx.Response(404, json={"detail": "no such card"})
    transport = httpx.MockTransport(lambda request: gone)
    with httpx.Client(transport=transport) as client, pytest.raises(Problem, match="no such card"):
        wait_for(client, "http://node", {}, "abc", timeout=5)
