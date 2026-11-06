# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The version is the
one `packages/common` reports, and every `pyproject.toml` carries it.

## [0.3.0] - 2026-11-06

A card through the whole mock stack: the api's pipeline, the comps, the photo
checks, the text with its checks, the model services in mock mode, and
`cardctl`.

### Added

- `POST /cards` (a photo, the stats as JSON, a style, and an optional face
  box), `GET /cards/{id}`, `GET /cards/{id}/front.png`, `back.png` and
  `preview.jpg`, and `DELETE /cards/{id}`.
- The pipeline. Cutout, art, comps and text run at the same time, then
  compose and render. Each step's row in the ledger holds what its service
  said about itself: model, licence, where it ran, milliseconds. A failed
  cutout leaves the background, failed art gets a drawn stadium, failed text
  gets a template. Only the render can fail a card. The front is written
  before the back, and the job lists each file as it lands.
- The photo: decoded with Pillow, EXIF dropped after its orientation is
  applied, scaled to 2048 px on the long side, 15 MB at most. The face box the
  browser sends is scaled with it.
- Photo checks from the matte (cut off at an edge, small in the frame, no
  subject) and from where the face landed on the card (placed low or high,
  face small or large, floats, tight photo). They go on the job and never fail
  a card.
- The text step: Gemma's prompt with three example backs, a JSON schema for
  vLLM's structured output, and `check()`, which turns down a back that
  praises, uses a number the facts do not hold, gives a side the player does
  not bat from, or names an angle for the wrong band. One retry names each
  problem, then a template writes the back from the stats. A test holds the
  prompt byte for byte by its sha256.
- `packages/comps`: the 225 qualified 2025 Statcast hitters, the
  nearest-neighbour lookup in z-scores, and `fetch_statcast.py`, which
  rebuilds the table from Baseball Savant by hand.
- SQLite in WAL mode for jobs and steps, the photos and cards on the data
  volume, and a sweeper that removes a card `RETENTION_HOURS` after it was
  made. A card cut off by a restart is marked failed when the api starts.
- `services/cutout`, `services/art` and `services/gpu-stats` with their mock
  backends, each behind the key. Caddy routes `/cutout`, `/art/generate` and
  `/gpu` straight to them.
- `cli/cardctl`: `cardctl make` posts a photo and stats, waits, writes both
  sides and `card.json`, and prints the ledger. The key comes from the
  environment or `.env`, never a flag.
- `make card-mock` and `make card PHOTO= STATS= STYLE= OUT=`.
- `examples/curl.md`, every route once.
- Tests: the routes, the pipeline and its fallbacks, five cards at once, a
  delete in the middle of a render, the database, the sweeper, the photo, the
  checks, the text and its 2,000 mock backs, the comps, `cardctl`, the writing
  rules on every doc, and the 401 matrix through Caddy for every route.

### Notes

- The mock stack stands in for the one-GPU node: every mock says `mock-gpu0`.
- The art service paints every background fresh until its pool lands with the
  real model. The api already asks for a pooled one and records which it got.

## [0.2.0] - 2026-11-06

The card, front and back, drawn on a laptop with no stack and no models.

### Added

- `packages/card`. `placement.py` is the face rule as arithmetic: the face 14
  percent of the picture tall and centred 27 percent down, grown up to 30
  percent until the person stands on the bottom edge, and the head never cut
  by the top edge. `compose.py` seals the matte, takes the wall out of its
  edge, fades the person where the photo cut them off, draws a ground shadow
  and pastes them on the art. `matte.py` fills holes and drops islands.
  `render.py` draws both sides at 1500 x 2100 in one Chromium, the front and
  its 500 x 700 preview first.
- Two styles. `akamai` is a navy frame and name plate with the Akamai logo,
  over a stadium at night. `classic` is a plain cream border with no logo,
  over a ballpark by day. Each is a front template and an entry in
  `styles.json` with its colours and the prompt its background is painted
  from.
- The back: the name and number, the bio, the nickname and tagline, the swing
  metrics, "Swings like", the scouting report and the ledger. Its last line
  claims Akamai Cloud GPUs only when every model step ran on one.
- `procedural.py`, the stand-ins for the two vision models: a stadium drawn
  with numpy, and a matte for a photo against a plain wall. `backgrounds.py`,
  the recipe each style's background is painted from.
- Instrument Sans under the SIL Open Font License, and the white Akamai logo.
  Both are inlined into the page, which asks the network for nothing. A card
  whose page tries fails.
- The test photo, `tests/fixtures/portrait.jpg` (CC0), its cutout and
  `SOURCES.md`, and `examples/stats.json`, the test player.
- `make card-fixture`, with `STYLE=`. `make test` installs Playwright's
  Chromium first.
- 215 tests for the card: the face rule, the edge over magenta, the shadow,
  the seal, the matte, both sides in Chromium, the front's boxes and its logo,
  two runs to the same bytes, and the browser kept between cards.

### Notes

- No light is painted round the person. A rim light read as a light line
  round the head on real photos, and the edge test over magenta fails on one.
- The README's card is drawn by the stand-ins, and its back says "Mock run. No
  GPU was used." The plain-wall matte bites into the test photo's right cheek.
  BiRefNet replaces it on the GPU node.

## [0.1.0] - 2026-11-06

The foundation: one key, one gateway, an api with three routes, and a stand-in
for vLLM. No card yet.

### Added

- A `uv` workspace: `packages/common`, `services/api` and `services/llm-mock`,
  locked in `uv.lock`. The images install what the lock holds.
- `packages/common`: settings, the bearer check with a limit of 20 failed tries
  a minute per address, the card schemas with the six swing metrics and their
  ranges, and an access log of method, path, status and milliseconds.
- `services/api`: `/livez` open, `/healthz` and `/models` behind the key. Any
  other path is 401 without the key.
- `services/llm-mock`: vLLM's `/v1/models` and `/v1/chat/completions`, its key
  check and its stream, with word lists where a model would be.
- `docker-compose.yml`, project `baseball-card`, profiles `mock` and `gpu`.
  Caddy is the only service with a port, on `127.0.0.1`, `GATEWAY_PORT`
  8443 by default.
- `Caddyfile` for tunnel mode and domain mode, and `docker-compose.domain.yml`,
  which swaps the loopback port for 80 and 443.
- `Makefile`: `help`, `env`, `up-mock`, `down`, `logs`, `ps`, `test`, `lint`.
  `DOCKER` names the docker command, `docker` by default.
- Tests: the 401 matrix through Caddy, the key in no container log and no build
  log, nothing shaped like a secret in any file git would commit, the compose
  ports, and Caddy in both modes.
- `LICENSE` (Apache-2.0), `CONTRIBUTING.md`, `docs/SECURITY.md`.
