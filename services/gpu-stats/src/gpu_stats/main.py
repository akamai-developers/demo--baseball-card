"""GET /gpu. What the node's cards are doing, once a second at most.

    BACKEND=real   runs nvidia-smi and parses the CSV
    BACKEND=mock   one invented card whose numbers drift, for a laptop

The api does not proxy this. Caddy routes /gpu straight here, so a panel
refreshing every two seconds never queues behind a card render. Same bearer key
as everything else.

nvidia-smi is not installed in this image. The NVIDIA container toolkit puts the
driver's own binary in the container when the compose entry asks for the
`utility` capability. If it is missing, or the call fails, the answer is an empty
list of GPUs and an error string, never a 500: a broken panel must not look like
a broken node.
"""

from __future__ import annotations

import asyncio
import math
import subprocess
import time

from fastapi import Depends

from common import __version__
from common.auth import require_api_key
from common.schemas import Gpu, GpuReport
from common.service import make_app
from common.settings import get_settings

QUERY = "index,name,utilization.gpu,memory.used,memory.total,temperature.gpu"
SMI = [
    "nvidia-smi",
    f"--query-gpu={QUERY}",
    "--format=csv,noheader,nounits",
]

# nvidia-smi takes about 50 ms and the panel polls every 2 s across every open
# tab. One reading a second is plenty and keeps a refresh loop off the driver.
CACHE_S = 1.0
TIMEOUT_S = 5.0

# What the mock pretends to have: the default layout's one card.
MOCK_GPU = (0, "Mock RTX 4000 Ada", 20475)

settings = get_settings()


def parse(csv: str) -> list[Gpu]:
    """nvidia-smi's CSV into rows. A line that does not parse is dropped."""
    gpus = []
    for line in csv.splitlines():
        fields = [field.strip() for field in line.split(",")]
        if len(fields) != 6:
            continue
        index, name, utilization, used, total, temperature = fields
        try:
            gpus.append(
                Gpu(
                    index=int(index),
                    name=name,
                    utilization_pct=int(float(utilization)),
                    memory_used_mb=int(float(used)),
                    memory_total_mb=int(float(total)),
                    temperature_c=int(float(temperature)),
                )
            )
        except ValueError:
            # [N/A] shows up on a card that does not report a field.
            continue
    return gpus


def read_smi() -> GpuReport:
    """Run nvidia-smi once. Blocking, so it is called in a thread."""
    try:
        result = subprocess.run(SMI, capture_output=True, text=True, timeout=TIMEOUT_S)
    except FileNotFoundError:
        return GpuReport(backend="real", error="nvidia-smi is not in this container")
    except subprocess.TimeoutExpired:
        return GpuReport(backend="real", error=f"nvidia-smi did not answer in {TIMEOUT_S:.0f}s")
    if result.returncode != 0:
        return GpuReport(backend="real", error=(result.stderr or "nvidia-smi failed").strip()[:200])
    return GpuReport(backend="real", gpus=parse(result.stdout))


def invent(now: float) -> GpuReport:
    """One card whose numbers move slowly, so the panel has something to draw.

    Nothing is measured here, and the backend field says so on every response.
    """
    index, name, total = MOCK_GPU
    phase = now / 7.0
    utilization = int(45 + 40 * math.sin(phase))
    used = int(total * (0.55 + 0.08 * math.sin(phase / 3.0)))
    gpu = Gpu(
        index=index,
        name=name,
        utilization_pct=max(0, min(100, utilization)),
        memory_used_mb=used,
        memory_total_mb=total,
        temperature_c=int(48 + 12 * math.sin(phase / 2.0)),
    )
    return GpuReport(backend="mock", gpus=[gpu])


class Cache:
    """One reading, at most CACHE_S seconds old."""

    def __init__(self) -> None:
        self.at = 0.0
        self.report = GpuReport(backend=settings.backend)
        self.lock = asyncio.Lock()

    async def get(self) -> GpuReport:
        if not settings.is_real:
            return invent(time.time())
        async with self.lock:
            now = time.monotonic()
            if now - self.at >= CACHE_S:
                self.report = await asyncio.to_thread(read_smi)
                self.at = now
            return self.report


cache = Cache()

app = make_app("baseball-card gpu-stats", __version__, settings)


@app.get("/gpu", response_model=GpuReport, dependencies=[Depends(require_api_key)])
async def gpu() -> GpuReport:
    """Every card this container can see. An empty list with an error is a fine answer."""
    return await cache.get()
