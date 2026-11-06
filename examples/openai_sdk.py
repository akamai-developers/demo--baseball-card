# /// script
# requires-python = ">=3.12"
# dependencies = ["openai>=1.60"]
# ///
"""Gemma 4 on the node, through the OpenAI SDK.

    uv run examples/openai_sdk.py
    uv run examples/openai_sdk.py "Scout a catcher with a strong arm."

vLLM speaks the OpenAI HTTP API, so the OpenAI SDK works against it with two
changes: base_url is this stack's gateway, and api_key is this stack's
CARD_API_KEY. Nothing goes to OpenAI.

The key and the gateway are read as cardctl reads them: the environment first,
then the nearest .env. The gateway is GATEWAY when you set it, https://DOMAIN
in domain mode, else localhost on GATEWAY_PORT, which is the mock stack or
make tunnel. Against the mock stack the answer comes from a word list, and it
says so.
"""

import os
import sys
from pathlib import Path

from openai import APIConnectionError, AuthenticationError, OpenAI

QUESTION = "Write a two-sentence scouting report for a shortstop with quick hands."


def setting(name: str) -> str:
    """From the environment, else from the first .env up from here that has it."""
    if value := os.environ.get(name, "").strip():
        return value
    for folder in (Path.cwd(), *Path.cwd().parents):
        dotenv = folder / ".env"
        if dotenv.is_file():
            for line in dotenv.read_text().splitlines():
                if line.startswith(f"{name}=") and line.split("=", 1)[1].strip():
                    return line.split("=", 1)[1].strip()
    return ""


def gateway() -> str:
    if url := setting("GATEWAY"):
        return url.rstrip("/")
    if domain := setting("DOMAIN"):
        return f"https://{domain}"
    return f"http://localhost:{setting('GATEWAY_PORT') or '8443'}"


def main() -> int:
    key = setting("CARD_API_KEY")
    if not key:
        print("no CARD_API_KEY. Run make env, or run this from the repo.", file=sys.stderr)
        return 1
    base = gateway()
    client = OpenAI(base_url=f"{base}/v1", api_key=key)

    try:
        # vLLM serves one model. Ask it for the name instead of typing it here.
        model = client.models.list().data[0].id
        print(f"{model} at {base}\n")
        stream = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": "You are a baseball scout. Answer in plain words."},
                {"role": "user", "content": " ".join(sys.argv[1:]) or QUESTION},
            ],
            max_tokens=200,  # the longest the answer may be, in tokens
            temperature=0.7,  # 0 gives the same answer every time
            stream=True,  # print the words as they arrive
        )
        for chunk in stream:
            if chunk.choices:
                print(chunk.choices[0].delta.content or "", end="", flush=True)
    except AuthenticationError:
        print(f"{base} refused CARD_API_KEY. Is it this stack's key?", file=sys.stderr)
        return 1
    except APIConnectionError:
        print(f"nothing answered at {base}. Run make up-mock, or make tunnel.", file=sys.stderr)
        return 1
    print()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
