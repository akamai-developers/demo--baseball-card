"""Calls from the api to the three model services.

The api sends the same bearer key the outside world sends, so the internal
Docker network is not a trust boundary. If the key is wrong here, the model
service answers 401 exactly as it would to a stranger.

Every reply from cutout and art carries the four X- headers from common.meta.
They go into the step ledger untouched, which is why a card can say which GPU
each model ran on. vLLM sends none, and the api fills those fields from its
own settings.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import httpx

from common.auth import auth_headers
from common.meta import ART_SOURCE, SOURCES, read_model_headers


class ServiceError(RuntimeError):
    """A model service refused, failed, or could not be reached."""


@dataclass(frozen=True)
class Reply:
    """Bytes from a model service, plus what it said about itself."""

    body: bytes
    meta: dict = field(default_factory=dict)


def _check(response: httpx.Response, name: str) -> None:
    if response.status_code == 401:
        raise ServiceError(f"{name} rejected the api key")
    if response.status_code >= 400:
        raise ServiceError(f"{name} answered HTTP {response.status_code}")


async def post_cutout(client: httpx.AsyncClient, base: str, key: str, photo: bytes) -> Reply:
    """POST the photo, get an RGBA PNG back."""
    response = await client.post(
        f"{base}/cutout",
        headers=auth_headers(key),
        files={"image": ("photo.jpg", photo, "image/jpeg")},
    )
    _check(response, "cutout")
    return Reply(response.content, read_model_headers(response.headers))


async def post_art(client: httpx.AsyncClient, base: str, key: str, payload: dict) -> Reply:
    """POST a prompt, get a PNG background back.

    The meta also carries `source`, pool or fresh, from the header only the art
    service sends. None when it sends no such header, or one that is neither.
    """
    response = await client.post(f"{base}/art/generate", headers=auth_headers(key), json=payload)
    _check(response, "art")
    meta = read_model_headers(response.headers)
    source = response.headers.get(ART_SOURCE)
    meta["source"] = source if source in SOURCES else None
    return Reply(response.content, meta)


async def post_chat(
    client: httpx.AsyncClient, base: str, key: str, payload: dict
) -> tuple[dict, dict]:
    """POST an OpenAI-compatible chat request. Returns (body, step metadata).

    vLLM does not send the X- headers, so the model id comes from the reply body.
    """
    response = await client.post(
        f"{base}/v1/chat/completions", headers=auth_headers(key), json=payload
    )
    _check(response, "llm")
    try:
        body = response.json()
    except ValueError as exc:
        raise ServiceError("llm did not return JSON") from exc
    meta = read_model_headers(response.headers)
    meta["model"] = meta["model"] or body.get("model")
    return body, meta


__all__ = ["Reply", "ServiceError", "post_art", "post_chat", "post_cutout"]
