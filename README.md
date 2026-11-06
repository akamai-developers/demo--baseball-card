# baseball-card

You give it a headshot and a name. Three open-weight models on one Akamai Cloud
GPU node make you a baseball card, front and back. Nothing is sent to any other
service.

<p>
  <img src="docs/card-front.jpg" width="49%" alt="The front of the test card: a man in a dark hoodie before a stadium at night, above a navy plate with 23, Sam Rivera, Shortstop and the Akamai logo">
  <img src="docs/card-back.jpg" width="49%" alt="The back of the test card: the bio, the nickname Short Fuse, six swing metrics, three comps, a scouting report and the ledger of every step">
</p>

That is the test photo's card, drawn by `make card-fixture` with stand-ins for
the models. The matte is a colour threshold, and it bit into the right cheek.
The stadium is drawn with numpy, and the comps and the words are samples. The
back says so on its last line.

BiRefNet cuts you out of the photo, FLUX.2 [klein] paints a stadium behind you,
and Gemma 4 writes your nickname and a scouting report from the facts you gave.
The back of the card lists which model ran each step, under which licence, on
which GPU, in how many milliseconds.

## status

This repo is being built, and this README says what works today.

| Part | State |
|---|---|
| The key, the gateway, `/livez`, `/healthz`, `/models` | built, tested in mock mode |
| The card, front and back, in two styles | built, drawn on a laptop with stand-ins for the models |
| The pipeline, the comps, the photo checks, `cardctl` | built, tested in mock mode |
| The cutout, art, gpu-stats and vLLM services, with stand-ins for the models | built, tested in mock mode |
| BiRefNet, FLUX.2 [klein] and Gemma 4 in those services | not built yet |
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
make card-mock    # the test photo's card, through the stack, into out/
```

If your user needs `sudo` for Docker, say so once per command:
`DOCKER="sudo -E docker" make up-mock`. If port 8443 is taken, set
`GATEWAY_PORT` in `.env`. The first `make up-mock` builds five images and takes
a few minutes. After that it takes seconds.

`make card-mock` posts `tests/fixtures/portrait.jpg` and `examples/stats.json`
to the stack, waits for the card, and prints the ledger:

```
step     model                            licence  ran on     ms   status
-------  -------------------------------  -------  ---------  ---  ------
cutout   mock/plain-wall-matte            n/a      mock-gpu0  221  done
art      mock/procedural-stadium · fresh  n/a      mock-gpu0  196  done
comps    nearest neighbour                n/a      mock       12   done
text     mock/wordlist                    n/a      mock-gpu0  15   done
compose  -                                -        mock       164  done
render   -                                -        mock       427  done

out/card-front.png
out/card-back.png
out/card-preview.jpg
out/card.json

2.1s total
```

That was on the 16-core box this repo was built on. Every model here is a
stand-in, and each one says so in the model column: `mock/plain-wall-matte` is
a colour threshold, `mock/procedural-stadium` draws a ballpark with numpy, and
`mock/wordlist` picks words from lists. The comps are real: the three 2025
hitters whose Statcast numbers sit nearest yours.

## your own card

```bash
uv run cardctl make --photo me.jpg --stats examples/stats.json --out out/
uv run cardctl make --photo me.jpg --stats examples/stats.json --style classic
make card PHOTO=me.jpg STATS=examples/stats.json STYLE=classic OUT=out
```

In mock mode use a photo against a plain wall, waist up. The stand-in for
BiRefNet keys out the wall by its colour and cannot cut a busy background.
`cardctl` reads the key from `CARD_API_KEY` in your environment or from `.env`.
It has no flag for the key, so the key never lands in your shell history.

The stats file needs a `name`. Everything else is optional: `number` (0 to 99),
`position` (`SS`, `CF`, up to 4 characters), `bats` (`R`, `L` or `S`),
`throws` (`R` or `L`), `height_in`, `weight_lb`, `hometown`, and up to six
numbers in `metrics`:

| Key | On the card | Range |
|---|---|---|
| `bat_speed_mph` | Bat Speed | 50 to 85 mph |
| `exit_velo_mph` | Exit Velocity | 60 to 115 mph |
| `launch_angle_deg` | Launch Angle | -10 to 35 deg |
| `swing_length_ft` | Swing Length | 4.5 to 10 ft |
| `attack_angle_deg` | Attack Angle | -5 to 25 deg |
| `time_to_contact_ms` | Time to Contact | 100 to 220 ms |

A card with no numbers has no stats table and no "Swings like". A number out of
its range is refused with a sentence that says the range.

Cutout, art, comps and text run at the same time. Then the cutout goes onto the
art, and Chromium draws both sides. The front is written first, so a caller
polling `GET /cards/{id}` can show it while the back is still drawing.

## the api

`/livez` is the one route open without the key:

```bash
curl -s -w '\n' http://localhost:8443/livez                               # ok
curl -s -o /dev/null -w '%{http_code}\n' http://localhost:8443/healthz   # 401
```

Everything else wants the key from `.env`:

```bash
export CARD_API_KEY=$(grep '^CARD_API_KEY=' .env | cut -d= -f2)
curl -s -w '\n' -H "Authorization: Bearer $CARD_API_KEY" http://localhost:8443/healthz
```

`/healthz` lists each model service as it describes itself.
[examples/curl.md](examples/curl.md) calls every route once: make a card, read
it, fetch both sides, delete it, and call each model on its own.

A card and its photo stay on the data volume for `RETENTION_HOURS`, 24 by
default. `DELETE /cards/{id}` removes them at once.

```bash
make test         # unit tests, and the integration tests against the stack
make down         # stop the stack. The volumes stay
make help         # every target
```

## the card

`make card-fixture` draws the test photo's card into `out/` with no stack and
no models: `card-front.png` and `card-back.png`, 1500 x 2100 each, and a
500 x 700 preview of the front. Playwright's Chromium draws both sides, and
the first run downloads it into Playwright's cache.

```bash
make card-fixture                  # the akamai style
make card-fixture STYLE=classic    # the classic style
```

On Linux, Chromium needs a few system libraries. If it will not start,
`uv run playwright install-deps chromium` installs them, with `sudo`.

A style is a front template, a palette and a prompt. `akamai` is a navy frame
with the Akamai logo, over a stadium at night. `classic` is a plain cream
border with no logo, over a ballpark by day. Each one is
`packages/card/src/card/templates/front-<style>.html` and an entry in
`styles.json`, which holds its colours and the prompt FLUX paints its
background from. The back is one template in the style's colours.

The face decides where you stand. Your face is 14 percent of the picture's
height, centred 27 percent of the way down, and you stand on the name plate.
A photo cut off at the waist makes the face bigger, up to 30 percent, so you
still reach the plate. The top edge never cuts your head. The image models
write no words on the card: every word is HTML.

The back has your bio, the nickname, the numbers you gave, the three hitters
nearest them, the scouting report and the ledger. The ledger names each
step's model, licence, where it ran and how many milliseconds it took. The
last line says "Made on Akamai Cloud GPUs" only when every model step ran on
a GPU.

## security

One key guards every route but `/livez`. Caddy is the only service with a
port, and it binds `127.0.0.1`. [docs/SECURITY.md](docs/SECURITY.md) lists
each claim with a command that checks it.

## layout

```
Makefile              every command here
docker-compose.yml    profiles mock and gpu
Caddyfile             the gateway, tunnel mode and domain mode
packages/common/      settings, the key check, schemas
packages/card/        the face rule, the compositor, the templates, the renderer
packages/comps/       the Statcast hitters and the nearest-neighbour lookup
services/api/         the orchestrator: routes, pipeline, text, photo checks
services/cutout/      the person out of the photo
services/art/         the stadium behind them
services/gpu-stats/   nvidia-smi as JSON
services/llm-mock/    vLLM's routes without vLLM
cli/                  cardctl
deploy/models.env     the model repos and their pinned commits
examples/             the test player's numbers, and every route with curl
tests/                unit, integration, security, and the test photo
```

## licence

Apache-2.0. See [LICENSE](LICENSE). Two files in the card are not covered by
it: the font is under the SIL Open Font License, beside it in
`packages/card/src/card/fonts/`, and the Akamai logo in
`packages/card/src/card/brand/` is Akamai's trademark. The test photo is CC0,
and `tests/fixtures/SOURCES.md` says where it came from. The hitters' table in
`packages/comps/src/comps/data/` is public MLB data from Baseball Savant, used
only to compare one set of numbers to another. Its first lines say where each
column came from.
