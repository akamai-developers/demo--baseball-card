# Build context is the repo root: docker build -f services/art/Dockerfile .
#
# The mock backend. No torch, no CUDA, no weights.
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

COPY pyproject.toml uv.lock /app/
COPY packages/common /app/packages/common
COPY packages/card /app/packages/card
COPY services/art /app/services/art
RUN uv sync --frozen --no-dev --no-editable --package card-art

RUN useradd --uid 10003 --create-home --shell /usr/sbin/nologin app
USER app

EXPOSE 8002
CMD ["uvicorn", "art.main:app", "--host", "0.0.0.0", "--port", "8002", "--no-access-log"]
