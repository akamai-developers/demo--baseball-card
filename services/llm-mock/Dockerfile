# Build context is the repo root: docker build -f services/llm-mock/Dockerfile .
#
# The laptop's stand-in for vLLM. The gpu profile runs the stock vllm/vllm-openai
# image instead and never builds this one.
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
COPY services/llm-mock /app/services/llm-mock
RUN uv sync --frozen --no-dev --no-editable --package card-llm-mock

RUN useradd --uid 10004 --create-home --shell /usr/sbin/nologin app
USER app

EXPOSE 8000
CMD ["uvicorn", "llm_mock.main:app", "--host", "0.0.0.0", "--port", "8000", "--no-access-log"]
