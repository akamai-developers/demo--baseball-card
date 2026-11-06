# Deploy to Akamai Cloud

One GPU node runs the three models behind one key. You reach it through an SSH
tunnel, or through a domain if you set one.

**It costs money from the first minute.** The node bills by the hour from
`make deploy` until `make destroy`, whether or not anything talks to it:

| `GPU_COUNT` | Plan | USD an hour |
|---|---|---|
| 1, the default | `g2-gpu-rtx4000a1-m` | 0.67 |
| 2 | `g2-gpu-rtx4000a2-s` | 1.05 |

Prices from the Linode API on 2026-11-06. `make gpu-check` prints today's.
Know how to stop the meter before you start it:

```bash
FORCE=1 make destroy
```

**This has not run against a real node yet.** Every script here was run
against stand-ins for Terraform, SSH and the Linode API, and `make gpu-check`
against the live API. The deploy itself, the tunnel and the GPU checks have
not. Where a number on this page was measured, it says when.

## what you need

- An Akamai Cloud account, and a personal access token that can read and
  write Linodes and Firewalls. Cloud Manager makes one under your profile, at
  API Tokens.
- `terraform` 1.7 or newer. On a Mac, `brew install hashicorp/tap/terraform`
  installs a current one.
- `ssh`, `ssh-keygen`, `rsync`, `curl`, `jq`, `openssl`, `bash` and
  [`uv`](https://docs.astral.sh/uv/). The scripts were run on Linux.

The scripts make the node's SSH key themselves, in `secrets/ssh/`.

## 1. set up

```bash
make env                      # writes .env with a new CARD_API_KEY, if there is none
export LINODE_TOKEN=...       # in your shell. Never in .env
```

Then set one line in `.env` by hand:

```
ALLOW_CLOUD_DEPLOY=1
```

Nothing makes, changes or deletes a cloud resource without it. `GPU_COUNT=1`
is already the default, and `LINODE_REGION=us-ord`.

## 2. find a region that can sell the plan

```bash
make gpu-check
```

It reads the plans, the regions and their availability from the Linode API,
and changes nothing. Availability changes during the day. Earlier on
2026-11-06 no region could sell an RTX 4000 Ada plan. At 22:28 UTC six could,
and the check printed, trimmed:

```
GPU_COUNT=1 deploys g2-gpu-rtx4000a1-m, RTX4000 Ada x1 Medium: 1 GPU, 8 vCPUs, 32 GB of RAM.

region        where             sells it now  USD an hour
------------  ----------------  ------------  -----------
de-fra-2      Frankfurt 2, DE   yes           0.67
fr-par        Paris, FR         yes           0.67
in-bom-2      Mumbai 2, IN      yes           0.67
jp-osa        Osaka, JP         yes           0.67
sg-sin-2      Singapore 2, SG   yes           0.67
us-ord        Chicago, IL       yes           0.67         <- LINODE_REGION
ap-south      Singapore, SG     no            0.67
...
us-southeast  Atlanta, GA       no            0.67

The other RTX 4000 Ada plans this repo can run on. LINODE_TYPE in .env picks one,
with GPU_COUNT set to its number of cards.

plan                 GPU_COUNT  vCPUs  RAM     USD an hour  sells it now in
-------------------  ---------  -----  ------  -----------  ----------------------------------------------------
g2-gpu-rtx4000a1-s   1          4      16 GB   0.52         de-fra-2, fr-par, in-bom-2, jp-osa, sg-sin-2, us-ord
...
g2-gpu-rtx4000a2-s   2          8      32 GB   1.05         de-fra-2, fr-par, in-bom-2, jp-osa, sg-sin-2, us-ord

us-ord can sell g2-gpu-rtx4000a1-m now. 6 of 22 GPU regions can.
```

If your region cannot sell it, set `LINODE_REGION` in `.env` to one that can.
`GPU_COUNT=2 make gpu-check` asks about the two-card plan. The 16 GB plan,
`g2-gpu-rtx4000a1-s`, is cheaper, but the one-card layout keeps FLUX's 16 GB
of weights in RAM, so it is not the default.

## 3. deploy

```bash
make deploy
```

It runs `make gpu-check` first and stops there if the region cannot sell the
plan, before anything is made. Then, in order:

1. a keypair for this node at `secrets/ssh/id_ed25519`, and the API key from
   `.env`
2. your public IPv4 address, from `whatismyip.akamai.com`, unless `ADMIN_CIDR`
   in `.env` names one. It is the only address the firewall lets reach port 22
3. `terraform apply`: one instance and one firewall, both tagged
   `baseball-card`
4. cloud-init on the node installs the NVIDIA driver, Docker and the NVIDIA
   container toolkit, then reboots. The deploy waits, and checks that
   `nvidia-smi` lists `GPU_COUNT` cards
5. the repo goes to `/opt/baseball-card` with `rsync`, less everything
   `.gitignore` names: `.env`, `secrets/`, the Terraform state
6. the node's own `.env`, mode 600, sent over SSH. It holds the key, the model
   pins and the GPU layout. `.env.example` lists every line
7. 27 GB of weights into the `hf-cache` volume: BiRefNet 0.4 GB, the folders
   FLUX.2 [klein] reads 16.0 GB, Gemma 4 10.3 GB
8. the torch base image, 15 GB, then the other images, then the stack
9. a wait until every service says its model is loaded

It is safe to run again. A second run changes nothing in Terraform, keeps the
key and the weights, and rebuilds only what changed. It ends with how long
each stage took, and:

```
the node is up  <ip>  tunnel mode  g2-gpu-rtx4000a1-m  0.67 USD an hour
```

`make gpu-logs` follows the driver install while it runs, then the
containers. `make ssh` opens a shell on the node.

## 4. open the tunnel

In tunnel mode, the default, only SSH is open, and Caddy listens on the node's
`127.0.0.1:8443`. Leave a forward running in a terminal of its own:

```bash
make tunnel
```

It runs `ssh -N -L 8443:127.0.0.1:8443 deploy@<ip>` with the repo's key, and
stops with ctrl-c. From a second terminal:

```bash
curl -s -w '\n' http://localhost:8443/livez    # ok
```

The tunnel takes `GATEWAY_PORT` on your machine, the port the mock stack uses.
If the mock stack is up, `make down` first.

## 5. check it

```bash
make check
```

It proves the key is enforced: `/v1/models`, `/healthz` and `/gpu` answer 401
with no key and with a wrong one, and 200 with it. Then that vLLM serves the
pinned Gemma 4, that every service is loaded on the card its layout gives it,
and that `nvidia-smi` on the node shows memory in use on each card. It reads
and changes nothing. It ends with `all checks passed`.

## 6. make a card

```bash
make smoke                                          # the test photo
make smoke PHOTO=me.jpg STATS=examples/stats.json   # yours
```

It makes a card on the node, writes both sides to `out/smoke/`, prints the
ledger, and fails unless both PNGs are 1500 x 2100 and cutout, art and text
each ran on a card that says `akamai-gpu`. A part made by a stand-in is a
fail.

The app is at http://localhost:8443 while the tunnel runs. Paste the key,
which this prints:

```bash
sed -n 's/^CARD_API_KEY=//p' .env
```

`make card PHOTO=me.jpg` and `uv run cardctl make` reach the node at the same
address.

## 7. call vLLM yourself

The node's vLLM is the stock OpenAI-compatible server. The key is the same
one:

```bash
export CARD_API_KEY=$(grep '^CARD_API_KEY=' .env | cut -d= -f2)
curl -s -w '\n' -H "Authorization: Bearer $CARD_API_KEY" http://localhost:8443/v1/models
curl -s -w '\n' -H "Authorization: Bearer $CARD_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model": "google/gemma-4-12B-it-qat-w4a16-ct", "max_tokens": 80,
       "messages": [{"role": "user", "content": "One sentence about a shortstop."}]}' \
  http://localhost:8443/v1/chat/completions
```

The same with the OpenAI SDK. `uv` installs it for this one script:

```bash
uv run examples/openai_sdk.py
uv run examples/openai_sdk.py "Scout a catcher with a strong arm."
```

It asks vLLM for the model's name, then streams the answer. Nothing goes to
OpenAI: `base_url` is the gateway and `api_key` is `CARD_API_KEY`. Both work
against the mock stack too, which answers from a word list and says so.

## 8. rotate the key

```bash
make rotate-key
```

It checks the current key works, writes a new one into the node's `.env`,
recreates the containers, then writes it into yours. It proves the old key
gets 401 from the api and from vLLM, and fails if either still takes it. An
open tunnel keeps working. An open browser tab signs out on its next call.

## 9. domain mode

A domain puts the gateway on the internet at `https://DOMAIN`, with a Let's
Encrypt certificate. The firewall then opens 80 and 443 to everyone, and the
key is the only thing between the internet and the models. The browser allows
the camera on HTTPS, so a phone can take the photo. Tunnel mode needs neither.

1. Set the name in `.env`, lower case:

   ```
   DOMAIN=cards.example.com
   ```

2. `make deploy`. With a node already up, the firewall gains 80 and 443 and the
   stack restarts with Caddy on them. After the apply it prints
   `point DNS at it now: cards.example.com A <ip>`.
3. At your DNS provider, make that A record. Caddy asks Let's Encrypt for a
   certificate when the stack starts, and keeps asking until the record
   resolves.
4. `make check` and `make smoke` use `https://DOMAIN` from then on. There is no
   tunnel in domain mode.

Let's Encrypt issues at most five certificates for the same name in seven
days. Caddy keeps its certificate in the `caddy-data` volume, so a second
`make deploy` asks for none, but `make destroy` deletes the volume with the
node. Deploy a new node with the same domain six times in a week, and the
sixth waits for Let's Encrypt to allow another, which can take more than a
day. A record that does not resolve yet costs failed tries, which Let's
Encrypt limits too, so set it before the stack starts.

Back to tunnel mode: set `DOMAIN=` empty and `make deploy`. The firewall
closes 80 and 443.

## 10. destroy

```bash
make destroy          # asks you to type the instance's label
FORCE=1 make destroy  # does not ask
```

It deletes the instance, its firewall and its disk, every card and photo on it
with them, then asks the API whether anything tagged `baseball-card` is left.
It ends with:

```
==> nothing tagged baseball-card is left. The meter is stopped.
```

It touches only what Terraform's state in `deploy/terraform/` holds. `.env`
and `secrets/ssh/` stay, and the next `make deploy` reuses both.

## when it goes wrong

| It says | Do this |
|---|---|
| `ALLOW_CLOUD_DEPLOY is not 1 in .env` | Step 1. |
| `<region> cannot sell the plan right now` | Pick a region from the table above it, or try later. Nothing was made. |
| `no ssh on <ip> after 10 minutes` | `ADMIN_CIDR` is not your public address: `curl -4 -s https://whatismyip.akamai.com`. Pin it in `.env` and deploy again. |
| `the node never reported ready` | The driver install failed or took over 30 minutes. `make ssh`, then `sudo tail -100 /var/log/baseball-card-bootstrap.log`. |
| `nvidia-smi lists 0 GPUs` | The driver did not load. The same log, then `make destroy` and deploy again. |
| `the stack did not report healthy` | A model is still loading or failed to. `make gpu-logs`. |
| A card's warning: `The art service is not answering, so the stadium is drawn instead of painted` | The card came out without that model. `make gpu-logs` says why, and `make check` shows which service is down. The same goes for the cutout service and the text model. |
| A card's warning: `The text model did not answer in 180 s` | vLLM is busy or stuck. `make gpu-logs`. `STEP_TIMEOUT_S` in `.env` sets how long a card waits for it, and `make deploy` sends a new one to the node. |
| `nothing answered http://localhost:8443/livez` | `make tunnel` in another terminal, or `DOMAIN` in `.env` for domain mode. |
| `something is already listening on localhost:8443` | The mock stack: `make down`. Or set `GATEWAY_PORT` to a free port. |
| `REMOTE HOST IDENTIFICATION HAS CHANGED` | The node's host key changed under the scripts, and they stop on purpose. If you replaced the node outside `make deploy`, remove its line from `secrets/ssh/known_hosts`. |
| `resource(s) tagged baseball-card may still be there` | Delete them in Cloud Manager. They bill by the hour. |
