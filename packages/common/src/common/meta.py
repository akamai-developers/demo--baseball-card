"""The four headers every model service puts on its response.

    X-Model      the model id, or a mock name when BACKEND=mock
    X-License    the licence that model ships under
    X-Infer-Ms   how long the inference itself took
    X-Ran-On     akamai-gpu0, akamai-gpu1, akamai-cpu, or a mock- label

The api copies these straight into the step ledger, so the line on the back of
the card is what the service reported about itself, not what the orchestrator
assumed. A service that says nothing leaves the field empty rather than getting
a guess written in for it.
"""

from __future__ import annotations

from collections.abc import Mapping

MODEL = "X-Model"
LICENSE = "X-License"
INFER_MS = "X-Infer-Ms"
RAN_ON = "X-Ran-On"

HEADER_NAMES = (MODEL, LICENSE, INFER_MS, RAN_ON)

# One more on a background from POST /art/generate: `pool` for one the art
# service painted before the card asked, `fresh` for one it painted for this
# request.
ART_SOURCE = "X-Art-Source"
SOURCES = ("pool", "fresh")


def model_headers(model: str, licence: str, infer_ms: float, ran_on: str) -> dict[str, str]:
    """Build the header dict. Services call this on every inference response."""
    return {
        MODEL: model,
        LICENSE: licence,
        INFER_MS: str(int(round(infer_ms))),
        RAN_ON: ran_on,
    }


def read_model_headers(headers: Mapping[str, str]) -> dict[str, object]:
    """Pull the four fields back out. Missing or unparseable values come back None.

    Header lookups are case-insensitive in httpx and in Starlette, which is the
    only place this is called from.
    """
    raw_ms = headers.get(INFER_MS)
    try:
        infer_ms = int(raw_ms) if raw_ms is not None else None
    except ValueError:
        infer_ms = None
    return {
        "model": headers.get(MODEL) or None,
        "license": headers.get(LICENSE) or None,
        "ms": infer_ms,
        "ran_on": headers.get(RAN_ON) or None,
    }


__all__ = [
    "ART_SOURCE",
    "HEADER_NAMES",
    "INFER_MS",
    "LICENSE",
    "MODEL",
    "RAN_ON",
    "SOURCES",
    "model_headers",
    "read_model_headers",
]
