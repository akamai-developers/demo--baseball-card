# The base image `cutout` and `art` share in the gpu profile. Python and torch,
# nothing else.
#
# Build context is the repo root:
#   docker build -f services/vision-base.Dockerfile -t baseball-card-vision-base:local .
#
# `make vision-base` does that, and the node needs it before compose builds.
# It is a separate build rather than a compose service because the two services
# that need it use it as a FROM, and compose does not order builds by FROM.
#
# CUDA 12.6 runtime on Ubuntu 24.04, and torch from the cu126 wheel index. Both
# are build arguments, so another CUDA is a flag away.
ARG BASE_IMAGE=nvidia/cuda:12.6.3-runtime-ubuntu24.04
FROM ${BASE_IMAGE}

ARG TORCH_INDEX=https://download.pytorch.org/whl/cu126
# The versions uv.lock holds, so everything else in the lock was resolved
# against this torch. tests/unit/test_vision_base.py holds that. They are pinned
# together: torchvision is built against one torch minor version and refuses to
# import next to another, and BiRefNet calls torchvision.ops.deform_conv2d.
ARG TORCH_VERSION=2.14.0
ARG TORCHVISION_VERSION=0.29.0

COPY --from=ghcr.io/astral-sh/uv:0.13.5 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PATH=/opt/venv/bin:$PATH \
    # The weights volume. Shared by cutout and art so each model downloads once.
    HF_HOME=/hf-cache

# The CUDA images carry no Python. The slim Python image carries no apt list
# worth updating. Install it only when it is missing, so one file serves both.
#
# python3-dev is here and not below because it is only right for an apt Python.
# The python:3.12-slim image already carries its own headers under
# /usr/local/include, and Debian's python3-dev would install a second, older
# interpreter next to the one this image is named after.
RUN if ! command -v python3 >/dev/null; then \
        apt-get update && \
        apt-get install -y --no-install-recommends \
            python3 python3-venv python3-dev ca-certificates && \
        rm -rf /var/lib/apt/lists/*; \
    fi

# torch ships Triton, and Triton compiles its kernels with a C compiler at run
# time, inside this container, the first time an op needs one. Without it the
# FLUX text encoder dies on its warm-up pass with:
#
#   RuntimeError: Failed to find C compiler. Please specify via CC environment
#   variable or set triton.knobs.build.impl.
#
# Measured on a GPU node on 2026-09-21. The CUDA runtime image has no compiler,
# and neither does python:3.12-slim. About 250 MB.
#
# `gcc` by name, not `cc`. Triton looks for gcc or clang and does not consult
# the alternatives symlink. The stub it compiles is a CPython extension that
# includes <Python.h>, and the headers come from the block above.
RUN if ! command -v gcc >/dev/null; then \
        apt-get update && \
        apt-get install -y --no-install-recommends gcc libc6-dev && \
        rm -rf /var/lib/apt/lists/*; \
    fi

# A venv rather than --system. Ubuntu 24.04 marks its Python externally managed
# (PEP 668), and a venv is the answer that does not involve arguing with it.
RUN uv venv --python python3 /opt/venv

RUN uv pip install --no-cache \
    --index-url "${TORCH_INDEX}" \
    "torch==${TORCH_VERSION}" "torchvision==${TORCHVISION_VERSION}"

WORKDIR /app
