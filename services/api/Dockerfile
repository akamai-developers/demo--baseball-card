# Build context is the repo root: docker build -f services/api/Dockerfile .
FROM python:3.12-slim-bookworm

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH

WORKDIR /app

# The lock, then the two packages this image runs. uv installs the versions
# uv.lock holds, and fails rather than resolve new ones.
COPY pyproject.toml uv.lock /app/
COPY packages/common /app/packages/common
COPY services/api /app/services/api
RUN uv sync --frozen --no-dev --no-editable --package card-api

# The model repos and their pinned commits, which GET /models reads.
COPY deploy/models.env /app/deploy/models.env

# The data volume is created with this ownership the first time it is mounted.
RUN useradd --uid 10001 --create-home --shell /usr/sbin/nologin app \
    && mkdir -p /data && chown app:app /data
USER app

EXPOSE 8080
CMD ["uvicorn", "api.main:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log"]
