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

**One route is open: `/livez`.** It says `ok` and nothing else. The browser
app's page and files are open too, and "the page" below says why. Every other
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

**vLLM gets the key as an environment variable, and cannot start without
one.** Compose hands vLLM `VLLM_API_KEY` from `CARD_API_KEY` in `.env`, and
refuses to render at all when that is empty, so vLLM never serves `/v1` open.
There is no `--api-key` flag, where the key would show in `ps`. vLLM is not
handed `.env`: its environment is the key, the cache path and `HF_TOKEN`.

```bash
uv run pytest tests/unit/test_compose_gpu.py -q -k "vllm"
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
`secrets/`, `*.tfstate*` and `*.tfvars` to `.gitignore`. It reads every commit
in the history the same way, message and patch, because a secret committed once
and deleted later is still in every clone. No file `.gitignore` names was ever
committed.

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
A bigger one gets a 413 and a sentence that says the limit.

```bash
grep -n 'max_size' Caddyfile
uv run pytest tests/integration/test_cards.py -q -k body_cap
```

## the models

**Every model is pinned to a commit.** `deploy/models.env` names each repo and
the 40-character commit the node downloads. BiRefNet matters most: it is loaded
with `trust_remote_code=True`, which runs the `birefnet.py` in its repo, so its
pin decides which Python runs. The cutout and art services hold the same pins
as constants and compose holds vLLM's, and a test holds all three to the file.

```bash
uv run pytest tests/unit/test_deploy_models.py -q
```

**No weights and no token are in an image.** The weights go into the
`hf-cache` volume when a service first loads them. `HF_TOKEN` is only for a
gated repo, and none of the pinned three is. It reaches a container as an
environment variable, never a build argument, so it is in no image layer.

```bash
grep -n 'HF_TOKEN' services/*/Dockerfile* services/vision-base.Dockerfile   # nothing
```

**Our images run as a user of their own.** The api, the model services and
gpu-stats each run as a numbered user with no shell, in the mock images and
the real ones. vLLM and Caddy run their published images as they come.

```bash
grep -n '^USER' services/*/Dockerfile*
```

**vLLM shares no memory with the host.** Its API server and engine pass data
through shared memory, a 2 GB `/dev/shm` of the container's own, not the
host's IPC namespace.

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

## the page

**The page is open, and every route it calls needs the key.** `/`, the files
under `/app/` and the face detector under `/vendor/` answer without the key,
because the key is typed into the page. Everything the page shows comes from
routes that need it. A file the page does not have is a 404, and a path with
`..` in it is never open.

```bash
curl -s -o /dev/null -w '%{http_code}\n' $GATEWAY/                               # 200
curl -s -o /dev/null -w '%{http_code}\n' --path-as-is $GATEWAY/app/../healthz   # 401
uv run pytest tests/unit/test_app_routes.py -q
```

**The key lives in one tab.** The page keeps it in `sessionStorage`, which
ends with the tab: no cookie, no `localStorage`. It goes on every request as
`Authorization: Bearer` and never in a URL. The card's pictures are fetched
with the header and shown from `blob:` URLs. When the node stops taking the
key, after a rotation, the tab signs out and says why. The browser test records
every request of a whole visit and checks each URL for the key.

```bash
uv run pytest tests/integration/test_app.py -q -k "key or sign"
```

**The page talks to its own origin and nowhere else.** No CDN, no font
service, no analytics. The font, the logo and the face detector come from the
node. The browser test checks the origin of every request, and this grep over
the page prints nothing:

```bash
grep -rnoE 'https?://[a-zA-Z0-9.-]+' web/app
```

How it works links to each model's repo. Those addresses come from
`GET /models`, open in a new tab, and send no referrer: the page sets
`<meta name="referrer" content="no-referrer">`.

**The face detector is pinned.** MediaPipe Tasks Vision and its BlazeFace
model are not in the repo. `web/vendor.lock` lists each file with its sha256.
The api image downloads them when it is built and checks every hash, and a
wrong hash fails the build.

```bash
uv run pytest tests/unit/test_web_vendor.py -q
make web-vendor && uv run python web/vendor.py --check
```

**A photo leaves the browser once, re-encoded.** The browser decodes it,
applies its EXIF rotation, and sends a new JPEG of 2048 px at most, with no
EXIF. The face is found in the browser and only its box goes with the photo.
The camera's frames stay in the page until the photo is taken. The api then
decodes and re-encodes the photo again, as the data section says.

## the node

These are built and tested here against a mock of the Linode provider and
stand-ins for SSH and the API. None has been checked on a real node yet. The
commands marked "on a node" are for after `make deploy`.

**Inbound is SSH from one address, and nothing else.** The firewall drops
every inbound packet no rule accepts. Tunnel mode has one rule: TCP 22 from
`ADMIN_CIDR`, your address as a `/32` when `.env` leaves it empty. Domain mode
adds TCP 80 and 443 from anywhere, for Let's Encrypt and browsers, and keeps 22
to the one address. The node has no private address. Outbound is open: the
node downloads its driver, images and weights.

```bash
uv run pytest tests/security/test_terraform.py -q
terraform -chdir=deploy/terraform output inbound_rules    # on a node
```

From outside `ADMIN_CIDR`, every port of a tunnel-mode node should time out.
From inside it, this should list 22 and nothing else. Not run yet:

```bash
IP=$(terraform -chdir=deploy/terraform output -raw ip_address)
for port in 22 80 443 8000 8001 8002 8003 8080 8443; do
  timeout 4 bash -c "</dev/tcp/$IP/$port" 2>/dev/null && echo "$port"
done
```

**The tunnel is the way in.** On the node Caddy binds `127.0.0.1:8443`, and
`make tunnel` runs `ssh -N -L 8443:127.0.0.1:8443 deploy@<ip>` with the
repo's key. SSH encrypts the traffic. Close the tunnel, and only SSH reaches
the node. The local end binds your machine's loopback only.

```bash
uv run pytest tests/unit/test_deploy_scripts.py -q -k tunnel
```

**SSH takes keys only, and no root.** cloud-init writes an sshd drop-in:
`PermitRootLogin no`, `PasswordAuthentication no`,
`KbdInteractiveAuthentication no`, `AuthenticationMethods publickey`, no agent
forwarding. No account has a password. The `deploy` user owns the repo and
runs Docker.

```bash
uv run pytest tests/security/test_terraform.py -q -k "sshd or cloud_init"
make ssh    # on a node, then: sudo sshd -T | grep -E 'permitrootlogin|passwordauthentication'
```

**The node's key is the repo's own, and its host key is pinned.**
`make deploy` makes `secrets/ssh/id_ed25519` the first time, mode 600, with no
passphrase, for this node and nothing else. Every script passes it with
`IdentitiesOnly=yes`, and keeps the node's host key in
`secrets/ssh/known_hosts`, never in `~/.ssh/`. The first connection learns the
host key, and every later one refuses a different key. Linode hands addresses
out again, so the deploy forgets an address's key only when Terraform has just
made or replaced the instance there.

```bash
uv run pytest tests/unit/test_deploy_scripts.py -q -k "forgets or forget or keeps"
git check-ignore -v secrets/ssh/id_ed25519
```

**The node's `.env` holds the key at mode 600, and the key is on no command
line.** `make deploy` builds the node's `.env` in memory and sends it on SSH's
stdin, into a file made under `umask 077`. Every script hands the key to
`curl` on stdin as a config line. On the node, scripts read it from the node's
own `.env`, or ask the api inside its container. The tests run every deploy
script against stand-ins and fail if the key or the Linode token was ever an
argument of any command.

```bash
uv run pytest tests/unit/test_deploy_scripts.py tests/unit/test_deploy_node_env.py -q
make ssh    # on a node, then: stat -c '%a %U' /opt/baseball-card/.env    # 600 deploy
```

**The node gets only what it needs.** Its `.env` is the key, the model pins,
the GPU layout, `DOMAIN`, and the defaults block of `.env`. `.env.example`
lists every line, and a test holds the list to what is written. Your
`ADMIN_CIDR`, `GATEWAY_PORT` and `ALLOW_CLOUD_DEPLOY` stay on your machine.
`HF_TOKEN` goes only from `.env`. One your shell exports for other work stays
on your machine.

**The Linode token never reaches the node.** Terraform reads it from your
shell. The scripts give it to `curl` on stdin. Terraform's state holds the
instance's settings and no secret: the API key never reaches Terraform. After a
deploy this prints 0:

```bash
grep -cFf <(sed -n 's/^CARD_API_KEY=//p' .env) deploy/terraform/terraform.tfstate
```

**A rotated key is dead.** `make rotate-key` writes a new key into the node's
`.env`, recreates the containers, writes it into yours, then fails unless the
old key gets 401 from the api and from vLLM.

```bash
uv run pytest tests/unit/test_deploy_scripts.py -q -k "rotat or old_key"
make rotate-key    # on a node
```

**Destroy deletes what the deploy made, and nothing else.** Terraform deletes
only what its state holds. Then `make destroy` asks the API for anything still
tagged `baseball-card`, and checks each row for that exact tag, so a resource
with any other tag is never counted, let alone touched. Nothing in this repo
deletes a resource through the API.

```bash
uv run pytest tests/unit/test_deploy_scripts.py tests/security/test_terraform.py -q -k "tag or destroy"
```

**Nothing is made by accident.** `make deploy`, `make rotate-key` and
`make destroy` refuse to run without `ALLOW_CLOUD_DEPLOY=1` in `.env`, before
any API call. `make deploy` asks the API whether the region can sell the plan
first, and stops before Terraform when it cannot.

```bash
uv run pytest tests/unit/test_deploy_scripts.py -q -k "refuses or gpu_check"
```

## what leaves the node

No photo and no stats. There is no provider model, and vLLM's usage report to
`stats.vllm.ai` is off: `VLLM_NO_USAGE_STATS=1` in its environment.

```bash
uv run pytest tests/unit/test_compose_gpu.py -q -k usage
```

The node calls out to build and to load:

- **Building:** Ubuntu's, Docker's and NVIDIA's apt repositories, Docker Hub
  and GitHub's container registry for the images, PyPI and PyTorch's index for
  the Python packages, Playwright's download of Chromium, and jsDelivr and
  Google Cloud Storage for the face detector's pinned files.
- **Loading:** Hugging Face, for the weights at their pinned commits, and for
  the files' metadata each time a service loads them.
- **Domain mode:** Let's Encrypt, for the certificate.

`make deploy` asks `whatismyip.akamai.com` for your address, unless
`ADMIN_CIDR` in `.env` names one.

## not covered

- One key for everyone. No accounts, scopes, expiry or audit log.
- Domain mode puts the gateway on the internet, and the key is its only guard.
- The first connection to a node trusts whatever host key answers. There is no
  other source for it.
- The `deploy` user has passwordless sudo. It is in the `docker` group, which
  is root on the node either way. The node is for this demo alone.
- Every container but vLLM and Caddy reads the whole `.env`: on the node, the
  key and `HF_TOKEN` when one is set, and on a laptop the deploy settings too.
- vLLM's published image runs as root inside its container.
- `make deploy` takes a setting from your shell before the line in `.env`,
  `HF_TOKEN` alone excepted, so `GPU_COUNT=2 make deploy` works for one run.
- Anything that runs in the page can read the key from `sessionStorage`, a
  browser extension included.
- `examples/curl.md` and `docs/DEPLOY.md` put the key on `curl`'s command line,
  as most API docs do. On a machine you share, use the scripts instead.
- Nothing above about the node has been checked on a real node yet.

## reporting a problem

Use GitHub's private vulnerability reporting on this repo: the Security tab,
then Report a vulnerability. Where that is not offered, open an issue that asks
for a private channel and leaves out the detail someone else could use. Never
put a key, a token or the address of a running node in an issue.
