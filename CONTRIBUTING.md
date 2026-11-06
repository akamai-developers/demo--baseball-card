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

## run the lint

```bash
make lint         # ruff check and ruff format --check
uv run ruff format .   # to fix the format
```

## the README's picture

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

## versions

The workspace has one version, `__version__` in
`packages/common/src/common/__init__.py`. A change that adds an entry to
`CHANGELOG.md` sets the same version there and in every `pyproject.toml`.
`tests/unit/test_version.py` fails until they agree.

## dependencies

Add a dependency to the `pyproject.toml` of the package that imports it, with a
lower bound, then run `uv lock` and commit `uv.lock`. The images install what
the lock holds.

## secrets

Never commit `.env`, anything under `secrets/`, Terraform state or `*.tfvars`.
`tests/security/test_repo_secrets.py` fails on a file shaped like a key.
