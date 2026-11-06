# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The version is the
one `packages/common` reports, and every `pyproject.toml` carries it.

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
