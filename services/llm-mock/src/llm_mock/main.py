"""vLLM's shape, without vLLM.

    GET  /v1/models
    POST /v1/chat/completions

Three things make this worth having rather than stubbing the client in the api:

  the same auth      vLLM checks `Authorization: Bearer` on paths that start
                     with /v1 and leaves /health open. This does the same, so
                     the 401 matrix in tests/security means something on a
                     laptop.
  the same failures  It answers with JSON in a string inside a chat completion,
                     which is where the api's parser earns its keep. It sends no
                     X-Model header, exactly as vLLM does not, so the api has to
                     get the licence and the GPU label from its own settings on
                     a laptop as well as on the node.
  the same stream    "stream": true gets server-sent events in vLLM's shape: a
                     chunk with the role, the reply a word at a time, a chunk
                     that says stop, then [DONE]. The OpenAI SDK reads it as it
                     reads vLLM's.

The text is picked from word lists by hashing the player's facts, so the same
player always gets the same back. The blurb reads the numbers out of those
facts, a sentence each for the first two, then one for a height, a weight or a
hometown when the facts hold one, as the facts write it, then a verdict. It is
not a language model and does not pretend to be one. Every card it writes says
mock/wordlist in the model column on the back.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import AsyncIterator
from typing import Any

from fastapi import Depends
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from common import __version__
from common.auth import require_api_key
from common.service import make_app
from common.settings import get_settings

MODEL_NAME = "mock/wordlist"

settings = get_settings()

# vLLM leaves /health open and only guards /v1. Caddy routes /v1/* and nothing
# else to this service, so /health is not reachable from outside anyway.
OPEN = frozenset({"/livez", "/health"})

NICKNAMES = [
    "Fast Twitch",
    "The Metronome",
    "Quick Draw",
    "Bottle Rocket",
    "Ice House",
    "The Hinge",
    "Night Shift",
    "Two Strike",
    "The Compass",
    "Cold Start",
    "Green Light",
    "The Turnstile",
    "Backspin",
    "Pocket Change",
    "Low Latency",
]

# Five words at most, so "<position>, <habit>" fits the tagline's limit.
HABITS = [
    "never cheats a rep",
    "shows up early",
    "runs everything out",
    "plays the whole nine",
    "last one off the field",
]

# A sentence for each measurement, by the label the facts give it. {n} is the
# number as the facts write it, less a trailing .0, the way a person writes it.
SENTENCES = {
    "Bat Speed": ("{first} swings at {n} mph.", "The bat moves at {n} mph."),
    "Exit Velocity": ("The ball comes off at {n} mph.", "Contact sends it out at {n} mph."),
    "Launch Angle": ("It leaves the bat at {n} degrees.", "A {n} degree launch angle."),
    "Swing Length": ("The swing covers {n} ft.", "A {n} ft path to the ball."),
    "Attack Angle": ("The bat comes up at {n} degrees.", "A {n} degree attack angle."),
    "Time to Contact": ("Bat meets ball in {n} ms.", "{n} ms from start to contact."),
}

# A sentence for a bio fact, by its label in the facts. {value} is the fact as
# the facts write it, "6 ft 1 in" or "Oakland, CA".
BIO = {
    "Height": "{first} stands {value}.",
    "Weight": "{first} weighs {value}.",
    "Hometown": "{first} calls {value} home.",
}
# The api's limit on the blurb, in words. The bio sentence is left out before
# the blurb goes past it.
BLURB_WORDS = 40

# The last sentence. What the numbers add up to, in five words or fewer. None
# names an angle's band or a side, because the mock does not read the angle or
# the side it would have to be right about.
VERDICTS = [
    "Gap to gap.",
    "Puts it in play.",
    "Singles and doubles.",
    "Tough to strike out.",
    "Works the count.",
]

# "Label: value", one to a line.
FACT = re.compile(r"^([^:\n]+):[ \t]*(\S.*)$", re.MULTILINE)
NUMBER = re.compile(r"-?\d+(?:\.\d+)?")
# The facts about the player. Every other line with a number is a measurement.
# Height and Weight start with a number and are still the player's.
PLAYER = frozenset({"Name", "Position", "Bats", "Throws", "Height", "Weight", "Hometown"})
WORD = re.compile(r"[^\W\d_]+")


class Message(BaseModel):
    role: str
    content: str | None = None


class ChatRequest(BaseModel):
    model: str = ""
    messages: list[Message] = Field(default_factory=list)
    temperature: float = 1.0
    max_tokens: int | None = None
    response_format: dict[str, Any] | None = None
    stream: bool = False


def _pick(options: list[str], digest: bytes, offset: int) -> str:
    return options[digest[offset] % len(options)]


def read_facts(facts: str) -> tuple[dict[str, str], list[tuple[str, str]]]:
    """The player's fields, and each measurement as (label, number), in order."""
    player: dict[str, str] = {}
    measured: list[tuple[str, str]] = []
    for label, value in FACT.findall(facts):
        label, value = label.strip(), value.strip()
        if label in PLAYER:
            player.setdefault(label, value)
        elif number := NUMBER.match(value):
            measured.append((label, number.group(0)))
    return player, measured


def nickname(name: str, digest: bytes) -> str:
    """A name off the list that shares no word with the player's own."""
    taken = {word for word in WORD.findall(name.casefold()) if len(word) >= 3}
    start = digest[0] % len(NICKNAMES)
    for step in range(len(NICKNAMES)):
        candidate = NICKNAMES[(start + step) % len(NICKNAMES)]
        if not taken & set(WORD.findall(candidate.casefold())):
            return candidate
    return NICKNAMES[start]


def tagline(position: str, digest: bytes) -> str:
    habit = _pick(HABITS, digest, 3)
    line = f"{position}, {habit}" if position else habit
    return line[:1].upper() + line[1:]


def blurb(
    name: str,
    measured: list[tuple[str, str]],
    digest: bytes,
    player: dict[str, str] | None = None,
) -> str:
    """A sentence for each of the first two numbers, then one for a bio fact
    when `player` holds one and it fits, then a verdict."""
    if not measured:
        return f"{name} sent no swing numbers, so this back has none."
    first = name.split()[0]
    sentences = []
    for index, (label, number) in enumerate(measured[:2]):
        n = number.removesuffix(".0")
        frame = _pick(list(SENTENCES[label]), digest, 4 + index)
        sentences.append(frame.format(first=first, n=n))
    verdict = _pick(VERDICTS, digest, 2)
    given = [label for label in BIO if (player or {}).get(label)]
    if player and given:
        label = given[digest[6] % len(given)]
        bio = BIO[label].format(first=first, value=player[label])
        if len(" ".join([*sentences, bio, verdict]).split()) <= BLURB_WORDS:
            sentences.append(bio)
    return " ".join([*sentences, verdict])


def card_text(facts: str) -> dict[str, str]:
    """Three fields, chosen from the word lists by hashing the player's facts."""
    digest = hashlib.sha256(facts.encode("utf-8")).digest()
    player, measured = read_facts(facts)
    # A label the lists do not know is not a swing number this card shows.
    measured = [(label, number) for label, number in measured if label in SENTENCES]
    name = player.get("Name", "The player")
    return {
        "nickname": nickname(name, digest),
        "tagline": tagline(player.get("Position", ""), digest),
        "blurb": blurb(name, measured, digest, player),
    }


def wants_json(request: ChatRequest) -> bool:
    """True when the caller asked for a schema, which is what the api does."""
    return bool(request.response_format) and request.response_format.get("type", "").startswith(
        "json"
    )


app = make_app("baseball-card llm-mock", __version__, settings, open_paths=OPEN)


@app.get("/health")
async def health() -> dict:
    """Open, exactly as vLLM's is. Caddy does not route it."""
    return {"status": "ok"}


@app.get("/v1/models", dependencies=[Depends(require_api_key)])
async def models() -> dict:
    """The list vLLM returns, with one entry. owned_by says this is the mock."""
    served = settings.llm_model or MODEL_NAME
    return {
        "object": "list",
        "data": [{"id": served, "object": "model", "created": 0, "owned_by": "mock"}],
    }


# A pause between streamed words, so a stream read by eye looks like one.
STREAM_GAP_S = 0.015
# A word and the space after it: what one streamed chunk carries.
PIECE = re.compile(r"\S+\s*")


async def stream_events(content: str, model: str, created: int) -> AsyncIterator[str]:
    """The reply as vLLM streams it, one server-sent event per chunk."""

    def event(delta: dict[str, str], finish: str | None = None) -> str:
        chunk = {
            "id": "chatcmpl-mock",
            "object": "chat.completion.chunk",
            "created": created,
            "model": model,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish}],
        }
        return f"data: {json.dumps(chunk)}\n\n"

    yield event({"role": "assistant", "content": ""})
    for piece in PIECE.findall(content):
        await asyncio.sleep(STREAM_GAP_S)
        yield event({"content": piece})
    yield event({}, "stop")
    yield "data: [DONE]\n\n"


@app.post("/v1/chat/completions", dependencies=[Depends(require_api_key)], response_model=None)
async def chat(request: ChatRequest) -> dict | StreamingResponse:
    """One completion. JSON when a schema was asked for, a sentence otherwise.
    Streamed as events when the request says "stream": true."""
    prompt = "\n".join(message.content or "" for message in request.messages)
    if wants_json(request):
        # The player's facts are the first user message. The system prompt names
        # the example players, so the whole prompt would find the wrong one.
        facts = next((m.content or "" for m in request.messages if m.role == "user"), "")
        content = json.dumps(card_text(facts))
    else:
        content = (
            "This is the mock language model. It answers so the stack can be run "
            "without a GPU. On the node this route is vLLM serving Gemma 4."
        )

    model = request.model or settings.llm_model or MODEL_NAME
    created = int(time.time())
    if request.stream:
        return StreamingResponse(
            stream_events(content, model, created), media_type="text/event-stream"
        )

    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": created,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": len(prompt.split()),
            "completion_tokens": len(content.split()),
            "total_tokens": len(prompt.split()) + len(content.split()),
        },
    }
