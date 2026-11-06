# Security

The node holds one secret, the API key. The laptop holds that key, the Linode
token in the shell, and an optional Hugging Face token. There is no bucket, no
third-party service and no provider model: a photo and the card made from it
stay on the node.

Each claim below has a command that checks it. Run them from the repo root
with the mock stack up:

```bash
make up-mock
GATEWAY=http://localhost:8443
```

## the key

**One key, `CARD_API_KEY`, and every service checks it.** The api, cutout, art
and gpu-stats use one FastAPI gate in `packages/common/src/common/auth.py`:
`Authorization: Bearer <key>`, compared with `hmac.compare_digest`, 401 with
`WWW-Authenticate: Bearer`. vLLM checks the same key itself, as
`VLLM_API_KEY`. The api sends the key on its own calls to the model services,
so the internal network is not a trust boundary. Caddy routes each model
service's one path, `/cutout`, `/art/generate`, `/v1/*` and `/gpu`, and
nothing else of theirs.

**One path is open: `/livez`.** It says `ok` and nothing else. Every other
path answers 401 without the key, one that does not exist included, so a
stranger learns nothing about the route list.

```bash
uv run pytest tests/security/test_gateway_auth.py -q
curl -s -o /dev/null -w '%{http_code}\n' $GATEWAY/healthz    # 401
curl -s -o /dev/null -w '%{http_code}\n' $GATEWAY/anything   # 401
```

**vLLM's open routes are not routed.** vLLM leaves `/health` and `/metrics`
open. Caddy sends vLLM `/v1/*` alone, where it checks the key, so those two
reach the api's gate instead.

```bash
curl -s -o /dev/null -w '%{http_code}\n' $GATEWAY/metrics    # 401
```

**A wrong key cannot be timed, and a short one cannot start.** The compare
takes the same time for any key. With `BACKEND=real` a service refuses a key
under 32 characters. `make env` makes one with `openssl rand -hex 32`.

**A guessing loop gets 429.** Over 20 failures from one address in a minute,
and that address gets 429 for a minute. The right key is always served and
clears the count. Each service counts in its own memory, and vLLM does not
count at all. It is a nuisance limit, not a defence.

```bash
uv run pytest tests/unit/test_auth.py tests/unit/test_auth_limit.py -q
```

**The key never reaches a log.** Each service logs method, path, status and
milliseconds, never a header, a body or a photo's file name. Caddy deletes the
request and response headers from its access log and from every other line it
writes. The test sends the key through every service and makes a card, then
reads every log the stack wrote and every file under `build-logs/`.

```bash
uv run pytest tests/security/test_no_key_in_logs.py -q
```

**The key stays off command lines.** `make env` writes it with shell builtins,
and `make up-mock` gives it to `curl` on stdin. `cardctl` reads it from the
environment or `.env` and has no flag for it.

**Nothing secret is in the repo.** A test reads every file git would commit for
anything shaped like a key, a token or a private key, and holds `.env`,
`secrets/`, `*.tfstate*` and `*.tfvars` to `.gitignore`.

```bash
uv run pytest tests/security/test_repo_secrets.py -q
git check-ignore -v .env secrets/ssh/id_ed25519 deploy/terraform/terraform.tfstate
```

## the network

**Caddy is the only service with a port, and it binds `127.0.0.1`.** The api
and the model services publish nothing. They live on the compose network.

```bash
uv run pytest tests/security/test_compose_ports.py -q
```

**Domain mode is its own file.** `docker-compose.domain.yml` replaces the
loopback port with 80 and 443 and hands Caddy the domain, which turns on Let's
Encrypt, the redirect from 80 and HSTS. It refuses to render without
`DOMAIN`. HSTS is sent on TLS connections only, never through a tunnel.

```bash
uv run pytest tests/security/test_domain_mode.py -q
```

**A request body is capped at 20 MB** by Caddy, before any service sees it.

```bash
grep -n 'max_size' Caddyfile
```

## the data

**A photo is re-encoded before anything reads it.** The api decodes the upload
with Pillow, applies its EXIF orientation, drops every EXIF tag, the GPS block
a phone writes included, scales the long side to 2048 px, and stores a JPEG of
its own. A file over 15 MB, or one Pillow cannot read, is refused with a
sentence before anything is stored.

```bash
uv run pytest tests/unit/test_photo.py -q
```

**A card lives 24 hours.** The sweeper removes a card, its photo and its
intermediates `RETENTION_HOURS` after it was made, 24 by default, and looks
every `SWEEP_EVERY_S`. `DELETE /cards/{id}` removes them at once. A card still
being made is stopped first and waited for, so nothing it was writing lands
after its files are gone. The integration test reads the data volume itself.

```bash
uv run pytest tests/unit/test_sweeper.py tests/unit/test_delete.py -q
uv run pytest tests/integration/test_cards.py -q -k delete
```

**A card id cannot reach outside the data volume.** Ids are 128-bit random
tokens, and anything with a slash or a dot is a 404 before it is joined to a
path.

```bash
uv run pytest tests/unit/test_storage.py -q -k escape
```

## the card

**The card's pages reach nothing.** Chromium draws each side from a page whose
font, stylesheet, logo and pictures are inlined or written beside it on the
local disk. A route handler refuses every http, https, ws and ftp request, and
a card whose page tried one fails instead of being written. A name or a word a
model wrote is escaped before it goes into the page.

```bash
uv run pytest tests/unit/test_card_render.py tests/unit/test_card_front.py -q -k "network or escaped"
```

## not built yet

These are the design. Each moves up to a claim with a command when it is built.

- The firewall: inbound TCP 22 from `ADMIN_CIDR` only, and 80 and 443 in domain
  mode. Everything else dropped.
- `make tunnel`: `ssh -N -L 8443:127.0.0.1:8443` with the repo's own key.
- The host: SSH keys only, root login and password login off, a `deploy` user,
  a keypair at `secrets/ssh/id_ed25519` and a pinned host key.
- The node's `.env` at mode 600, written over SSH, with the key on no command
  line on the node.
- `make rotate-key`, and a 401 for the old key after it.

## not covered

- One key for everyone. No accounts, scopes, expiry or audit log.
- Every container reads the whole `.env`. On a laptop that is the key, the
  deploy settings and `HF_TOKEN` when one is set.

## reporting a problem

Use GitHub's private vulnerability reporting on this repo: the Security tab,
then Report a vulnerability. Where that is not offered, open an issue that asks
for a private channel and leaves out the detail someone else could use. Never
put a key, a token or the address of a running node in an issue.
