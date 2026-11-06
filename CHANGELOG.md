# Changelog

All notable changes to this project. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/). The version is the
one `packages/common` reports, and every `pyproject.toml` carries it.

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
