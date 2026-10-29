"""Tests that run on any platform — pure logic, no registry access."""

from __future__ import annotations

import json

from startupmgr.models import ALL_SOURCES, StartupEntry
from startupmgr.render import render_csv, render_json, render_table, truncate
from startupmgr.scanner import filter_entries
from startupmgr.winapi import split_command


def make(**kw) -> StartupEntry:
    base = dict(
        name="TestApp",
        source="registry-user-run",
        location=r"HKCU\Software\Microsoft\Windows\CurrentVersion\Run",
        scope="user",
        command="C:\\Program Files\\App\\app.exe --minimized",
        exe=r"C:\Program Files\App\app.exe",
        args="--minimized",
        exists=True,
        enabled=True,
    )
    base.update(kw)
    return StartupEntry(**base)


# ---------------------------------------------------------------- models
def test_id_is_stable_and_short():
    e = make()
    assert len(e.id) == 10
    assert e.id == make().id


def test_id_changes_with_identity():
    a, b = make(name="A"), make(name="B")
    assert a.id != b.id


def test_id_is_case_insensitive():
    assert make(name="App").id == make(name="app").id


def test_to_dict_roundtrip_drops_raw():
    e = make(raw={"secret": "x"})
    d = e.to_dict()
    assert "raw" not in d
    assert d["id"] == e.id
    restored = StartupEntry.from_dict(d)
    assert restored.name == e.name
    assert restored.id == e.id


def test_all_sources_has_expected_count():
    assert len(ALL_SOURCES) == 10
    assert "scheduled-task" in ALL_SOURCES
    assert "service" in ALL_SOURCES


# ---------------------------------------------------------------- winapi
def test_split_quoted_path():
    exe, args = split_command('"C:\\Program Files\\App\\a.exe" -x 1')
    assert exe == r"C:\Program Files\App\a.exe"
    assert args == "-x 1"


def test_split_unquoted_with_spaces():
    exe, args = split_command(r"C:\Program Files\App\a.exe -x")
    assert exe == r"C:\Program Files\App\a.exe"
    assert args == "-x"


def test_split_bare_exe():
    exe, args = split_command(r"C:\Windows\notepad.exe")
    assert exe == r"C:\Windows\notepad.exe"
    assert args == ""


def test_split_with_env_var(monkeypatch):
    monkeypatch.setenv("MYDIR", r"C:\Tools")
    exe, _ = split_command(r"%MYDIR%\tool.exe")
    assert exe == r"C:\Tools\tool.exe"


def test_split_empty():
    assert split_command("") == ("", "")
    assert split_command("   ") == ("", "")


def test_split_strips_start_prefix():
    exe, _ = split_command('start "" "C:\\a\\b.exe"')
    assert "b.exe" in exe


# ---------------------------------------------------------------- render
def test_truncate_respects_width():
    assert truncate("abcdefghij", 5) == "abcd…"
    assert truncate("abc", 10) == "abc"


def test_truncate_cjk_width():
    # Each CJK char is 2 columns; width 5 -> fits 2 chars + ellipsis
    assert truncate("中文测试", 5) == "中文…"


def test_render_table_contains_entries():
    out = render_table([make()])
    assert "TestApp" in out
    assert "registry-user-run" not in out  # label is shown, not raw id
    assert "HKCU Run" in out


def test_render_table_empty():
    assert "no matching" in render_table([])


def test_render_table_marks_missing_file():
    out = render_table([make(exists=False, exe=r"C:\gone.exe")])
    assert "missing file" in out


def test_render_table_com_handler_placeholder():
    out = render_table([make(source="scheduled-task", command="", exe="", exists=False)])
    assert "COM handler" in out
    assert "missing file" not in out


def test_render_json_valid():
    payload = json.loads(render_json([make()]))
    assert payload["count"] == 1
    assert payload["entries"][0]["name"] == "TestApp"


def test_render_csv_header():
    out = render_csv([make()])
    header = out.splitlines()[0]
    assert header.startswith("id,name,source")


# ---------------------------------------------------------------- filters
def test_filter_by_scope():
    entries = [make(scope="user"), make(name="M", scope="machine")]
    assert len(filter_entries(entries, scope="machine")) == 1


def test_filter_by_search_matches_publisher():
    entries = [make(publisher="Microsoft Corporation"), make(name="Other")]
    assert len(filter_entries(entries, search="microsoft")) == 1


def test_filter_enabled_only():
    entries = [make(), make(name="Off", enabled=False)]
    assert len(filter_entries(entries, enabled_only=True)) == 1


def test_filter_disabled_only():
    entries = [make(), make(name="Off", enabled=False)]
    got = filter_entries(entries, disabled_only=True)
    assert len(got) == 1 and got[0].name == "Off"


def test_filter_missing_only():
    entries = [make(), make(name="Gone", exists=False)]
    assert len(filter_entries(entries, missing_only=True)) == 1


def test_filter_by_source():
    entries = [make(), make(name="Svc", source="service")]
    assert len(filter_entries(entries, sources=["service"])) == 1


# ---------------------------------------------------------------- approved blob
def test_decode_approved_states():
    from startupmgr.collectors.registry import decode_approved

    assert decode_approved(None) == (True, True)
    assert decode_approved(b"\x02" + b"\x00" * 11) == (True, True)
    assert decode_approved(b"\x06" + b"\x00" * 11) == (True, True)
    assert decode_approved(b"\x03" + b"\x00" * 11) == (False, True)
    enabled, known = decode_approved(b"\x09")
    assert enabled is True and known is False
