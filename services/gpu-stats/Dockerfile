# Build context is the repo root: docker build -f services/gpu-stats/Dockerfile .
#
# No CUDA, no torch, no weights. nvidia-smi is not in this image: the NVIDIA
# container toolkit puts the driver's own binary into the container at run time,
# which is what the `utility` capability in docker-compose.yml asks for. In the
# mock profile nothing is put there and the service answers with made-up numbers.
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
COPY services/gpu-stats /app/services/gpu-stats
RUN uv sync --frozen --no-dev --no-editable --package card-gpu-stats

RUN useradd --uid 10005 --create-home --shell /usr/sbin/nologin app
USER app

EXPOSE 8003
CMD ["uvicorn", "gpu_stats.main:app", "--host", "0.0.0.0", "--port", "8003", "--no-access-log"]
