# baseball-card

You give it a headshot and a name. Three open-weight models on one Akamai Cloud
GPU node make you a baseball card, front and back. Nothing is sent to any other
service.

BiRefNet cuts you out of the photo, FLUX.2 [klein] paints a stadium behind you,
and Gemma 4 writes your nickname and a scouting report from the facts you gave.
The back of the card lists which model ran each step, under which licence, on
which GPU, in how many milliseconds.

## status

This repo is being built, and this README says what works today.

| Part | State |
|---|---|
| The key, the gateway, `/livez`, `/healthz`, `/models` | built, tested in mock mode |
| A stand-in for vLLM with the same routes and key check | built, tested in mock mode |
| The card, the pipeline, the comps, `cardctl` | not built yet |
| BiRefNet, FLUX.2 [klein] and Gemma 4 in their services | not built yet |
| The browser app | not built yet |
| The deploy to Akamai Cloud | not built yet |

## models

| Step | Model | Licence |
|---|---|---|
| cutout | [`ZhengPeng7/BiRefNet`](https://huggingface.co/ZhengPeng7/BiRefNet) | MIT |
| art | [`black-forest-labs/FLUX.2-klein-4B`](https://huggingface.co/black-forest-labs/FLUX.2-klein-4B) | Apache 2.0 |
| text | [`google/gemma-4-12B-it-qat-w4a16-ct`](https://huggingface.co/google/gemma-4-12B-it-qat-w4a16-ct) on vLLM | Apache 2.0 |

`deploy/models.env` pins each one to a commit.

## quick start

Mock mode runs every service in Docker with stand-ins for the models. No GPU,
no cloud account. It needs Docker with Compose 2.24 or newer, `make`, `curl`,
`openssl` and [`uv`](https://docs.astral.sh/uv/).

```bash
git clone https://github.com/akamai-developers/demo--baseball-card.git
cd demo--baseball-card
make up-mock      # writes .env with a new key, builds, serves localhost:8443
```

If your user needs `sudo` for Docker, say so once per command:
`DOCKER="sudo -E docker" make up-mock`. If port 8443 is taken, set
`GATEWAY_PORT` in `.env`.

`/livez` is the one route open without the key:

```bash
curl -s -w '\n' http://localhost:8443/livez                               # ok
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8443/healthz   # 401
```

Everything else wants the key from `.env`:

```bash
export CARD_API_KEY=$(grep '^CARD_API_KEY=' .env | cut -d= -f2)
curl -s -w '\n' -H "Authorization: Bearer $CARD_API_KEY" http://localhost:8443/healthz
curl -s -w '\n' -H "Authorization: Bearer $CARD_API_KEY" http://localhost:8443/models
curl -s -w '\n' -H "Authorization: Bearer $CARD_API_KEY" http://localhost:8443/v1/models
```

`/healthz` lists each model service as it describes itself. In mock mode the
text model is `mock/wordlist`, which picks words from lists and says so.

```bash
make test         # unit tests, and the integration tests against the stack
make down         # stop the stack. The volumes stay
make help         # every target
```

## security

One key guards every route but `/livez`. Caddy is the only service with a
port, and it binds `127.0.0.1`. [docs/SECURITY.md](docs/SECURITY.md) lists
each claim with a command that checks it.

## layout

```
Makefile             every command here
docker-compose.yml   profiles mock and gpu
Caddyfile            the gateway, tunnel mode and domain mode
packages/common/     settings, the key check, schemas
services/api/        the orchestrator
services/llm-mock/   vLLM's routes without vLLM
deploy/models.env    the model repos and their pinned commits
tests/               unit, security
```

## licence

Apache-2.0. See [LICENSE](LICENSE).
