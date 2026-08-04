from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolated_state(monkeypatch, tmp_path):
    monkeypatch.setenv("S16_DATA_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("S16_A2A_GRPC_ENABLED", "0")
    monkeypatch.setenv("S16_SANDBOX_ROOT", str(tmp_path / "sandbox"))
    (tmp_path / "sandbox").mkdir()


@pytest.fixture
def app_client():
    from fastapi.testclient import TestClient

    from s16code.main import app

    with TestClient(app) as client:
        yield client
