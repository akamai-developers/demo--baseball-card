"""The stand-in for vLLM: its routes, its key check, its stream, its word lists."""

from __future__ import annotations

import importlib
import json

import pytest
from fastapi.testclient import TestClient

from common.schemas import METRICS

KEY = "t" * 64
AUTH = {"Authorization": f"Bearer {KEY}"}
WRONG = {"Authorization": "Bearer " + "w" * 64}
FACTS = (
    "Name: Sam Rivera\nPosition: SS\nHometown: Chicago, IL\n"
    "Bat Speed: 68.2 mph\nExit Velocity: 88.0 mph"
)


@pytest.fixture
def llm(clean_settings) -> TestClient:
    """Re-import the service so it picks up the settings this test just set."""
    module = importlib.reload(importlib.import_module("llm_mock.main"))
    with TestClient(module.app) as client:
        yield client


def ask_for_json(facts: str) -> dict:
    return {
        "model": "m",
        "messages": [
            {"role": "system", "content": "Name: Example Player\nBat Speed: 74.9 mph"},
            {"role": "user", "content": facts},
        ],
        "response_format": {"type": "json_schema", "json_schema": {"name": "card_text"}},
    }


def test_it_guards_v1_and_leaves_health_open_like_vllm(llm: TestClient) -> None:
    assert llm.get("/health").status_code == 200
    assert llm.get("/livez").status_code == 200
    assert llm.get("/v1/models").status_code == 401
    assert llm.get("/v1/models", headers=WRONG).status_code == 401
    assert llm.post("/v1/chat/completions", json={}).status_code == 401


def test_it_lists_one_model_and_says_it_is_the_mock(llm: TestClient) -> None:
    body = llm.get("/v1/models", headers=AUTH).json()
    assert body["object"] == "list"
    assert [entry["id"] for entry in body["data"]] == ["mock/wordlist"]
    assert body["data"][0]["owned_by"] == "mock"


def test_it_returns_json_when_a_schema_was_asked_for(llm: TestClient) -> None:
    body = llm.post("/v1/chat/completions", headers=AUTH, json=ask_for_json(FACTS)).json()
    written = json.loads(body["choices"][0]["message"]["content"])
    assert set(written) == {"nickname", "tagline", "blurb"}
    assert written["tagline"].startswith("SS, ")


def test_the_blurb_reads_the_players_facts_and_not_the_system_prompts(llm: TestClient) -> None:
    body = llm.post("/v1/chat/completions", headers=AUTH, json=ask_for_json(FACTS)).json()
    blurb = json.loads(body["choices"][0]["message"]["content"])["blurb"]
    assert "68.2" in blurb
    # 88.0 is written the way a person writes it.
    assert "88 mph" in blurb and "88.0" not in blurb
    assert "74.9" not in blurb
    assert "Example" not in blurb


def test_a_player_with_no_numbers_is_told_so(llm: TestClient) -> None:
    body = llm.post(
        "/v1/chat/completions", headers=AUTH, json=ask_for_json("Name: Sam Rivera")
    ).json()
    blurb = json.loads(body["choices"][0]["message"]["content"])["blurb"]
    assert blurb == "Sam Rivera sent no swing numbers, so this back has none."


def test_the_nickname_shares_no_word_with_the_name() -> None:
    from llm_mock.main import NICKNAMES, nickname

    for word in ("Twitch", "Metronome", "Rocket"):
        chosen = nickname(f"Sam {word}", bytes(32))
        assert word.lower() not in chosen.lower()
    assert nickname("Sam Rivera", bytes(32)) == NICKNAMES[0]


def test_every_swing_number_has_its_sentences() -> None:
    """The facts label each number as METRICS does. A label the mock had no
    sentence for would drop that number from every mock back."""
    from llm_mock.main import SENTENCES

    assert set(SENTENCES) == {metric.label for metric in METRICS.values()}


def test_it_is_deterministic_for_the_same_prompt(llm: TestClient) -> None:
    first = llm.post("/v1/chat/completions", headers=AUTH, json=ask_for_json(FACTS)).json()
    again = llm.post("/v1/chat/completions", headers=AUTH, json=ask_for_json(FACTS)).json()
    assert first["choices"] == again["choices"]


def test_without_a_schema_it_answers_in_plain_words(llm: TestClient) -> None:
    request = {"messages": [{"role": "user", "content": "hello"}]}
    body = llm.post("/v1/chat/completions", headers=AUTH, json=request).json()
    assert "mock language model" in body["choices"][0]["message"]["content"]


def events_of(text: str) -> list[str]:
    """A server-sent event stream as the payload of each `data:` line, in order."""
    blocks = [block for block in text.split("\n\n") if block.strip()]
    assert all(block.startswith("data: ") for block in blocks), blocks
    return [block.removeprefix("data: ") for block in blocks]


@pytest.mark.parametrize(
    "request_body",
    [
        {"model": "any/name", "messages": [{"role": "user", "content": "hello"}]},
        {**ask_for_json(FACTS), "model": "any/name"},
    ],
    ids=["words", "json"],
)
def test_it_streams_the_same_reply_when_asked_to(llm: TestClient, request_body: dict) -> None:
    """The OpenAI SDK reads chunks in vLLM's shape and stops at [DONE]."""
    whole = llm.post("/v1/chat/completions", headers=AUTH, json=request_body).json()
    streamed = llm.post("/v1/chat/completions", headers=AUTH, json={**request_body, "stream": True})
    assert streamed.status_code == 200
    assert streamed.headers["content-type"].startswith("text/event-stream")

    events = events_of(streamed.text)
    assert events[-1] == "[DONE]"
    chunks = [json.loads(event) for event in events[:-1]]
    assert {chunk["object"] for chunk in chunks} == {"chat.completion.chunk"}
    assert {chunk["model"] for chunk in chunks} == {"any/name"}
    assert chunks[0]["choices"][0]["delta"] == {"role": "assistant", "content": ""}
    assert chunks[-1]["choices"][0] == {"index": 0, "delta": {}, "finish_reason": "stop"}
    pieces = [chunk["choices"][0]["delta"].get("content", "") for chunk in chunks]
    assert len([piece for piece in pieces if piece]) > 3
    assert "".join(pieces) == whole["choices"][0]["message"]["content"]


def test_it_wants_the_key_for_a_stream_too(llm: TestClient) -> None:
    request = {"messages": [{"role": "user", "content": "hello"}], "stream": True}
    assert llm.post("/v1/chat/completions", json=request).status_code == 401
    assert llm.post("/v1/chat/completions", headers=WRONG, json=request).status_code == 401


def test_it_sends_no_model_headers_because_vllm_does_not(llm: TestClient) -> None:
    """The api has to get the licence and the GPU label from its own settings."""
    response = llm.get("/v1/models", headers=AUTH)
    assert "X-Model" not in response.headers
    assert "X-License" not in response.headers
    assert "X-Ran-On" not in response.headers
