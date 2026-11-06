# Contributing

A change ships with its test. A bug fix comes with the test that failed before
it, and a new route comes with its line in the 401 matrix in
`tests/security/test_gateway_auth.py`.

## run the tests

```bash
uv sync           # the workspace and its dev tools, from uv.lock
make test         # every test but the slow ones
```

`make test` installs Playwright's Chromium first, once, into Playwright's own
cache: the card's tests draw real cards with it. On Linux, if Chromium will
not start, `uv run playwright install-deps chromium` installs the system
libraries it needs, with `sudo`.

The unit tests run anywhere. The integration tests need the mock stack, and
skip with a reason when it is not up:

```bash
make up-mock
make test
```

The compose and Caddy tests call Docker. Where your user needs `sudo` for it,
run `DOCKER="sudo -E docker" make test`. `-E` matters: the tests set variables
for compose, and plain `sudo` drops them.

If 8443 is taken on your machine, set `GATEWAY_PORT` in `.env`. The stack, the
Makefile and the tests all read it.

## the real models

The tests run the BiRefNet and FLUX code with the model stubbed, so they need
no torch and no weights. One test runs BiRefNet for real, on your CPU:

```bash
make test-slow
```

It adds torch and BiRefNet's other dependencies to `.venv` the first time,
about 1 GB, and the weights to Hugging Face's cache in your home directory,
444 MB. Then it takes about a minute. `make card-cpu-cutout` runs the same
model inside the mock stack and makes a card with it.

FLUX and Gemma 4 do not run on a laptop. `make vision-base` builds the CUDA
torch image the node's cutout and art images start from, 15 GB, if you want to
see a change to those images build before it reaches a GPU.

## the browser tests

`tests/integration/test_app.py` and `tests/integration/test_app_camera.py`
run the page in Playwright's Chromium against the mock stack. The api serves
the page from its image, so after a change under `web/app/` run `make up-mock`
again before the tests. They write screenshots to `out/ui/` at 1920 x 1080 and
390 x 844: look at them after a change to the layout. The camera tests play the
test photo as a fake camera, made with `ffmpeg`, and skip without it.

To run the api from a checkout rather than from its image, `make web-vendor`
first. It downloads the face detector into `web/vendor/`, which git ignores.

## the deploy scripts

No test calls the live Linode API or makes a cloud resource.
`tests/unit/test_deploy_scripts.py` runs every script in `deploy/scripts/`
against stand-ins for Terraform, SSH, rsync and the API, and fails if the key
or the Linode token is ever an argument of a command.
`tests/unit/test_gpu_check.py` reads the API's recorded answers in
`tests/fixtures/linode/`. `tests/fixtures/SOURCES.md` says when and how they
were recorded. `tests/security/test_terraform.py` runs `terraform test`, which
plans against a mock of the Linode provider. It needs `terraform` 1.7 or newer,
and its first run downloads the provider the lock file pins.

## run the lint

```bash
make lint         # ruff, shellcheck, terraform fmt and terraform validate
uv run ruff format .   # to fix the format
terraform -chdir=deploy/terraform fmt -recursive   # to fix the HCL
```

`make lint` needs `shellcheck` and `terraform` besides `uv`.

## the pictures

`docs/card-front.jpg` and `docs/card-back.jpg` are the two PNGs of
`make card-fixture` at half size. A change that shows on either side of the
card makes them again:

```bash
make card-fixture
uv run python -c "
from PIL import Image
for side in ('front', 'back'):
    Image.open(f'out/card-{side}.png').convert('RGB').resize((750, 1050), Image.LANCZOS).save(
        f'docs/card-{side}.jpg', quality=88, optimize=True, progressive=True)"
```

The style picker shows the front of the test card in each style,
`web/app/styles/<style>.jpg` at 192 x 268:

```bash
for style in akamai classic; do
  make card-fixture STYLE=$style
  uv run python -c "
from PIL import Image
Image.open('out/card-front.png').convert('RGB').resize((192, 268), Image.LANCZOS).save(
    'web/app/styles/$style.jpg', quality=85, optimize=True, progressive=True)"
done
```

`docs/app.jpg` is the browser test's screenshot of the test card on the mock
stack, at two thirds:

```bash
make up-mock
uv run pytest tests/integration/test_app.py -q -k test_photo
uv run python -c "
from PIL import Image
Image.open('out/ui/result-front-1920x1080.png').convert('RGB').resize((1280, 720), Image.LANCZOS).save(
    'docs/app.jpg', quality=85, optimize=True, progressive=True)"
```

## versions

The workspace has one version, `__version__` in
`packages/common/src/common/__init__.py`. A change that adds an entry to
`CHANGELOG.md` sets the same version there and in every `pyproject.toml`.
`tests/unit/test_version.py` fails until they agree.

## the writing rules

Every word a person reads is held to them: the README, the docs, the examples,
the changelog, the card templates, the text model's prompt and the sentences
the api sends back. `tests/unit/test_copy.py` reads those files.

- No em-dashes. Use a period or a comma.
- None of the words listed in `BANNED` in `services/api/src/api/writing.py`.
  They are the words that make a page read like any company's blog.
- Short sentences, one idea each, with numbers and file names over adjectives.
- Say what was only tested in mock mode.

## dependencies

Add a dependency to the `pyproject.toml` of the package that imports it, with a
lower bound, then run `uv lock` and commit `uv.lock`. The images install what
the lock holds.

torch is the exception. The lock holds the CPU build from PyTorch's CPU index,
and `services/vision-base.Dockerfile` installs the CUDA build of the same
version on the node. A torch upgrade changes both, and
`tests/unit/test_vision_base.py` fails until they agree.

## secrets

Never commit `.env`, anything under `secrets/`, Terraform state or `*.tfvars`.
`tests/security/test_repo_secrets.py` fails on a file shaped like a key.
