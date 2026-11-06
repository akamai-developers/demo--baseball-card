"""Health fan-out. The api asks each model service how it is doing, with the key."""

from __future__ import annotations

import asyncio

import httpx

from common.auth import auth_headers
from common.schemas import ServiceHealth

PROBE_TIMEOUT_S = 5.0


def _from_payload(payload: dict) -> ServiceHealth:
    """Map a model service's own /healthz body onto ServiceHealth.

    The service's own status wins. A service whose weights failed to load says
    so, and the api must not turn that into a cheerful "still loading".
    """
    loaded = bool(payload.get("loaded"))
    reported = payload.get("status")
    if reported not in {"ok", "loading", "error"}:
        reported = "ok" if loaded else "loading"
    ran_on = payload.get("ran_on")
    return ServiceHealth(
        status=reported,
        model=payload.get("model"),
        backend=payload.get("backend"),
        device=payload.get("device"),
        gpu=payload.get("gpu"),
        loaded=loaded,
        vram_mb=payload.get("vram_mb"),
        error=payload.get("error"),
        ran_on=ran_on if isinstance(ran_on, str) and ran_on else None,
        pool_ready=_count(payload.get("pool_ready")),
        pool_size=_count(payload.get("pool_size")),
    )


def _count(value: object) -> int | None:
    """A whole number of backgrounds, or None. bool is an int in Python, and not one of these."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


async def probe(
    client: httpx.AsyncClient, name: str, url: str, key: str, llm_ran_on: str | None = None
) -> ServiceHealth:
    """Ask one service. vLLM has no /healthz of ours, so it is asked for its model list.

    vLLM does not say which card it is on either. `llm_ran_on` is what the stack
    was told, the same label the text step's ledger row gets. Its list says who
    serves the model: vllm on the node, mock on a laptop.
    """
    headers = auth_headers(key)
    try:
        if name == "llm":
            response = await client.get(f"{url}/v1/models", headers=headers)
            response.raise_for_status()
            served = response.json().get("data") or []
            model = served[0].get("id") if served else None
            return ServiceHealth(
                status="ok" if model else "loading",
                model=model,
                backend=served[0].get("owned_by") if served else None,
                loaded=bool(model),
                ran_on=llm_ran_on or None,
            )
        response = await client.get(f"{url}/healthz", headers=headers)
        response.raise_for_status()
        return _from_payload(response.json())
    except httpx.HTTPStatusError as exc:
        return ServiceHealth(status="error", error=f"HTTP {exc.response.status_code}")
    except (httpx.HTTPError, ValueError, AttributeError) as exc:
        return ServiceHealth(status="unreachable", error=type(exc).__name__)


async def probe_all(
    urls: dict[str, str], key: str, llm_ran_on: str | None = None
) -> dict[str, ServiceHealth]:
    """Probe every configured service at once. An empty map means nothing is configured."""
    if not urls:
        return {}
    async with httpx.AsyncClient(timeout=PROBE_TIMEOUT_S) as client:
        names = list(urls)
        results = await asyncio.gather(*(probe(client, n, urls[n], key, llm_ran_on) for n in names))
    return dict(zip(names, results, strict=True))
