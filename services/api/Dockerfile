# Build context is the repo root: docker build -f services/api/Dockerfile .
#
# This image carries Chromium, because the api renders the card itself. That is
# most of its size. The model services are separate images and do not have it.
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH \
    # Outside the root home directory, so the unprivileged user can read it.
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

WORKDIR /app

# The lock, then the packages this image runs. uv installs the versions uv.lock
# holds, and fails rather than resolve new ones.
COPY pyproject.toml uv.lock /app/
COPY packages/common /app/packages/common
COPY packages/card /app/packages/card
COPY packages/comps /app/packages/comps
COPY services/api /app/services/api
RUN uv sync --frozen --no-dev --no-editable --package card-api

# Chromium and its shared libraries, at the build the locked Playwright wants.
# --with-deps runs apt-get, so it has to happen while this is still root.
RUN playwright install --with-deps chromium \
    && chmod -R a+rX /opt/playwright \
    && rm -rf /var/lib/apt/lists/*

# The model repos and their pinned commits, which GET /models reads.
COPY deploy/models.env /app/deploy/models.env

# The data volume is created with this ownership the first time it is mounted.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data
USER app

EXPOSE 8080
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
