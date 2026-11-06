"""The gpu-stats service: the CSV parser, both backends, and the key."""

from __future__ import annotations

import subprocess

import pytest
from fastapi.testclient import TestClient

from tests.conftest import TEST_KEY

AUTH = {"Authorization": f"Bearer {TEST_KEY}"}

REAL_CSV = (
    "0, NVIDIA RTX 4000 Ada Generation, 74, 19968, 20475, 61\n"
    "1, NVIDIA RTX 4000 Ada Generation, 3, 18984, 20475, 45\n"
)


@pytest.fixture
def service(clean_settings, monkeypatch):
    """A fresh import of the service, so settings are the test's own."""
    import sys

    for name in [name for name in sys.modules if name.startswith("gpu_stats")]:
        del sys.modules[name]
    import gpu_stats.main as service_module

    return service_module


@pytest.fixture
def client(service) -> TestClient:
    with TestClient(service.app) as test_client:
        yield test_client


# -- the parser --------------------------------------------------------------


def test_it_parses_what_nvidia_smi_prints(service) -> None:
    gpus = service.parse(REAL_CSV)
    assert len(gpus) == 2
    assert gpus[0].index == 0
    assert gpus[0].name == "NVIDIA RTX 4000 Ada Generation"
    assert gpus[0].utilization_pct == 74
    assert gpus[0].memory_used_mb == 19968
    assert gpus[0].memory_total_mb == 20475
    assert gpus[0].temperature_c == 61
    assert gpus[1].index == 1


def test_a_field_that_is_not_available_drops_the_row(service) -> None:
    """A card that reports [N/A] for a field is left out rather than reported as
    zero, which would read as an idle GPU."""
    gpus = service.parse("0, Card, [N/A], 100, 200, 40\n1, Card, 10, 100, 200, 40\n")
    assert [gpu.index for gpu in gpus] == [1]


def test_junk_parses_to_nothing(service) -> None:
    assert service.parse("") == []
    assert service.parse("no GPUs were found\n") == []


# -- the real backend --------------------------------------------------------


def test_the_real_backend_shells_out(service, monkeypatch) -> None:
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=REAL_CSV, stderr="")

    monkeypatch.setattr(service.subprocess, "run", fake_run)
    report = service.read_smi()
    assert report.backend == "real"
    assert len(report.gpus) == 2
    assert calls[0][0] == "nvidia-smi"
    assert "--format=csv,noheader,nounits" in calls[0]


def test_no_nvidia_smi_is_an_error_string_not_a_crash(service, monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        raise FileNotFoundError(cmd)

    monkeypatch.setattr(service.subprocess, "run", fake_run)
    report = service.read_smi()
    assert report.gpus == []
    assert "nvidia-smi" in report.error


def test_a_failing_nvidia_smi_is_an_error_string(service, monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        return subprocess.CompletedProcess(cmd, 9, stdout="", stderr="driver mismatch")

    monkeypatch.setattr(service.subprocess, "run", fake_run)
    report = service.read_smi()
    assert report.gpus == []
    assert "driver mismatch" in report.error


def test_a_hanging_nvidia_smi_gives_up(service, monkeypatch) -> None:
    def fake_run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, service.TIMEOUT_S)

    monkeypatch.setattr(service.subprocess, "run", fake_run)
    assert "did not answer" in service.read_smi().error


# -- the mock backend --------------------------------------------------------


def test_the_mock_backend_invents_the_default_nodes_one_card(service) -> None:
    """One RTX 4000 Ada, as GPU_COUNT=1 deploys, and the mocks' labels say
    mock-gpu0 to match."""
    report = service.invent(1000.0)
    assert report.backend == "mock"
    assert [gpu.index for gpu in report.gpus] == [0]
    for gpu in report.gpus:
        assert 0 <= gpu.utilization_pct <= 100
        assert 0 < gpu.memory_used_mb < gpu.memory_total_mb
        assert "Mock" in gpu.name


def test_the_mock_numbers_move(service) -> None:
    first = service.invent(1000.0).gpus[0].utilization_pct
    later = service.invent(1010.0).gpus[0].utilization_pct
    assert first != later


# -- the route ---------------------------------------------------------------


def test_gpu_without_a_key_is_401(client: TestClient) -> None:
    response = client.get("/gpu")
    assert response.status_code == 401
    assert response.headers["WWW-Authenticate"] == "Bearer"


def test_gpu_with_a_wrong_key_is_401(client: TestClient) -> None:
    assert client.get("/gpu", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_gpu_with_the_key_answers(client: TestClient) -> None:
    response = client.get("/gpu", headers=AUTH)
    assert response.status_code == 200
    body = response.json()
    assert body["backend"] == "mock"
    assert len(body["gpus"]) == 1
    assert body["error"] is None


def test_livez_is_open(client: TestClient) -> None:
    assert client.get("/livez").status_code == 200


def test_an_unknown_path_fails_closed(client: TestClient) -> None:
    assert client.get("/nope").status_code == 401
