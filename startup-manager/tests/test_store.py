"""Tests for snapshot / restore / audit storage."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from startupmgr import store
from startupmgr.models import StartupEntry


@pytest.fixture(autouse=True)
def temp_home(tmp_path, monkeypatch):
    monkeypatch.setenv("STARTUPMGR_HOME", str(tmp_path))
    yield tmp_path


def make(**kw) -> StartupEntry:
    base = dict(
        name="App",
        source="registry-user-run",
        location=r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
        scope="user",
        command=r"C:\App\app.exe",
        exe=r"C:\App\app.exe",
        exists=True,
    )
    base.update(kw)
    return StartupEntry(**base)


def test_data_dir_created(temp_home):
    d = store.data_dir()
    assert d.exists() and str(d) == str(temp_home)


def test_snapshot_roundtrip(temp_home):
    entries = [make(), make(name="B")]
    path = store.save_snapshot(entries, label="test")
    assert path.exists()

    loaded = store.load_snapshot(path)
    assert len(loaded) == 2
    assert {e.name for e in loaded} == {"App", "B"}


def test_snapshot_payload_shape(temp_home):
    path = store.save_snapshot([make()])
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["tool"] == "startupmgr"
    assert payload["count"] == 1
    assert payload["label"] == ""


def test_latest_snapshot(temp_home):
    assert store.latest_snapshot() is None
    first = store.save_snapshot([make()], label="first")
    second = store.save_snapshot([make()], label="second")
    assert store.latest_snapshot() in (first, second)
    assert len(store.list_snapshots()) == 2


def test_audit_appends(temp_home):
    store.audit("delete", make(), "test-detail")
    store.audit("enable", make())
    log = store.data_dir() / "audit.log"
    lines = log.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2
    rec = json.loads(lines[0])
    assert rec["action"] == "delete"
    assert rec["detail"] == "test-detail"
    assert rec["id"] == make().id


def test_load_snapshot_rejects_garbage(temp_home):
    bad = Path(temp_home) / "bad.json"
    bad.write_text('{"not": "a snapshot"}', encoding="utf-8")
    with pytest.raises(ValueError):
        store.load_snapshot(bad)


def test_quarantine_dir_created(temp_home):
    assert store.quarantine_dir().exists()


def test_registry_entry_present_detection(temp_home):
    # On non-Windows, _registry_value_exists returns True (nothing to do).
    e = make()
    present = store._entry_present(e)
    assert isinstance(present, bool)


def test_restore_is_idempotent_when_present(temp_home):
    e = make()
    if os.name != "nt":
        # Off-Windows the presence check short-circuits to True.
        with pytest.raises(store.AlreadyRestored):
            store.restore_entry(e)
