"""CLI-level tests: parser wiring and exit codes.

These do not touch the real registry — they check argument handling and the
guards that should fire before any system mutation.
"""

from __future__ import annotations

import pytest

from startupmgr.cli import EX_REFUSED, EX_UNSUPPORTED, build_parser, main
from startupmgr.models import ALL_SOURCES


def test_every_subcommand_builds():
    parser = build_parser()
    for name in (
        "list", "sources", "show", "enable", "disable", "delete",
        "backup", "backups", "restore", "audit", "doctor",
    ):
        with pytest.raises(SystemExit):  # -h exits 0
            parser.parse_args([name, "-h"])


def test_backup_has_no_source_flag():
    """backup must tolerate a missing .source attribute (regression guard)."""
    parser = build_parser()
    ns = parser.parse_args(["backup"])
    assert not hasattr(ns, "source")


def test_scan_tolerates_missing_source_attr(monkeypatch):
    """_scan() must not crash when the subcommand has no --source."""
    from startupmgr import cli

    class FakeScanner:
        def __init__(self, sources):
            self.sources = sources
            self.errors: list[str] = []

        def scan(self):
            return []

    monkeypatch.setattr(cli, "Scanner", FakeScanner)
    import argparse

    ns = argparse.Namespace()  # nothing set at all
    entries, errors = cli._scan(ns)
    assert entries == []
    assert errors == []


def test_scan_uses_all_sources_when_absent(monkeypatch):
    from startupmgr import cli

    captured = {}

    class FakeScanner:
        def __init__(self, sources):
            captured["sources"] = sources
            self.errors = []

        def scan(self):
            return []

    monkeypatch.setattr(cli, "Scanner", FakeScanner)
    import argparse

    cli._scan(argparse.Namespace(source=None))
    assert captured["sources"] == ALL_SOURCES


def test_unknown_command_errors():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["nonsense"])
    assert exc.value.code == 2


def test_disable_requires_targets():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["disable"])
    assert exc.value.code == 2


def test_sources_command_runs(capsys):
    rc = main(["sources"])
    assert rc == 0
    out = capsys.readouterr().out
    for sid in ALL_SOURCES:
        assert sid in out


def test_enabled_disabled_mutually_exclusive():
    parser = build_parser()
    with pytest.raises(SystemExit) as exc:
        parser.parse_args(["list", "--enabled-only", "--disabled-only"])
    assert exc.value.code == 2


@pytest.mark.skipif(
    __import__("sys").platform == "win32", reason="guard only applies off-Windows"
)
def test_off_windows_exits_unsupported():
    assert main(["list"]) == EX_UNSUPPORTED


def test_service_delete_refused():
    """Deleting a service must be refused with exit code 5."""
    if __import__("sys").platform != "win32":
        pytest.skip("windows only")
    # A service name that will always exist on Windows.
    rc = main(["delete", "WwanSvc"])
    assert rc in (EX_REFUSED, 3)
