"""Pytest fixtures: isolate every test from the user's real cityops DB."""

from __future__ import annotations

import os

import pytest


@pytest.fixture(autouse=True)
def _isolate_db(tmp_path, monkeypatch):
    """Point cityops at a per-test temp SQLite file."""
    db = tmp_path / "test.sqlite"
    monkeypatch.setenv("CITYOPS_DB_PATH", str(db))
    monkeypatch.setenv("CITYOPS_DATA_DIR", str(tmp_path / "csv"))
    os.makedirs(tmp_path / "csv", exist_ok=True)
    yield
