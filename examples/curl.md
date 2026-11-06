# Every route, with curl

Each route once, in the order a card goes through them. Run these from the repo
root with the stack up: `make up-mock` on a laptop, or `make tunnel` open to the
node in another terminal. The key comes from `.env`, the gateway from `G`.

```bash
export CARD_API_KEY=$(grep '^CARD_API_KEY=' .env | cut -d= -f2)
G=http://localhost:8443
mkdir -p out
```

If you set `GATEWAY_PORT` in `.env`, use that port in `G`.

## open

`/livez` is the only route that answers without the key.

```bash
curl -s -w '\n' $G/livez
```

```
ok
```

The browser app is open too, because the key is typed into it: the page at
`/`, its files under `/app/`, and the face detector under `/vendor/`. A file
it does not have is a 404:

```bash
curl -s -o /dev/null -w '%{http_code}\n' $G/                  # 200
curl -s -o /dev/null -w '%{http_code}\n' $G/app/app.js        # 200
curl -s -o /dev/null -w '%{http_code}\n' $G/app/no-such-file  # 404
```

Every other route answers 401 without the key, a path that does not exist
included:

```bash
curl -s -o /dev/null -w '%{http_code}\n' $G/healthz
curl -s -o /dev/null -w '%{http_code}\n' $G/no-such-path
```

## the api

What each model service says about itself, and the version:

```bash
curl -s -w '\n' -H "Authorization: Bearer $CARD_API_KEY" $G/healthz
```

The three models and the commit each is pinned to:

```bash
curl -s -w '\n' -H "Authorization: Bearer $CARD_API_KEY" $G/models
```

Make a card. The photo is a file, the stats are the JSON of
`examples/stats.json` as a string, and the style is `akamai` or `classic`. The
answer is 202 and a card id:

```bash
CARD=$(curl -s -H "Authorization: Bearer $CARD_API_KEY" \
  -F photo=@tests/fixtures/portrait.jpg \
  -F stats="$(cat examples/stats.json)" \
  -F style=akamai \
  $G/cards | python3 -c 'import json, sys; print(json.load(sys.stdin)["card_id"])')
echo $CARD
```

The job: its status, every step of the ledger, the text, the comps, what the
photo checks found, and which files are ready. In mock mode it is done in about
two seconds:

```bash
sleep 3
curl -s -w '\n' -H "Authorization: Bearer $CARD_API_KEY" $G/cards/$CARD
```

Both sides and the preview, into `out/`:

```bash
curl -s -H "Authorization: Bearer $CARD_API_KEY" -o out/front.png $G/cards/$CARD/front.png
curl -s -H "Authorization: Bearer $CARD_API_KEY" -o out/back.png $G/cards/$CARD/back.png
curl -s -H "Authorization: Bearer $CARD_API_KEY" -o out/preview.jpg $G/cards/$CARD/preview.jpg
```

Remove the card, its photo and everything made from it. The answer is 204, and
the card is 404 from then on:

```bash
curl -s -o /dev/null -w '%{http_code}\n' -X DELETE \
  -H "Authorization: Bearer $CARD_API_KEY" $G/cards/$CARD
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $CARD_API_KEY" $G/cards/$CARD
```

## each model on its own

Every model answers on its own path, with the same key. Each answer carries
`X-Model`, `X-License`, `X-Infer-Ms` and `X-Ran-On`, which is where the ledger
on the back of the card comes from. `-D -` prints them.

The person out of a photo, as an RGBA PNG:

```bash
curl -s -D - -H "Authorization: Bearer $CARD_API_KEY" \
  -F image=@tests/fixtures/portrait.jpg -o out/cutout.png $G/cutout
```

A background from a prompt:

```bash
curl -s -D - -H "Authorization: Bearer $CARD_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"prompt": "an empty baseball stadium at night", "style": "akamai", "seed": 7}' \
  -o out/art.png $G/art/generate
```

The language model, OpenAI-compatible. On the node this is vLLM serving
Gemma 4. In mock mode it is a word list that says it is one:

```bash
curl -s -w '\n' -H "Authorization: Bearer $CARD_API_KEY" $G/v1/models
curl -s -w '\n' -H "Authorization: Bearer $CARD_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model": "mock/wordlist", "messages": [{"role": "user", "content": "Say hello."}]}' \
  $G/v1/chat/completions
```

On the node, the model is the `id` that `/v1/models` lists. The OpenAI SDK
works the same way, with this stack's key as its `api_key`:

```bash
uv run examples/openai_sdk.py
```

What the GPUs are doing. In mock mode it invents one card and says
`"backend": "mock"`:

```bash
curl -s -w '\n' -H "Authorization: Bearer $CARD_API_KEY" $G/gpu
```
