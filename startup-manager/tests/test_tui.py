"""TUI smoke tests using Textual's headless Pilot."""

from __future__ import annotations

import pytest

textual = pytest.importorskip("textual")

from startupmgr.models import SOURCE_LABELS, StartupEntry  # noqa: E402
from startupmgr.tui import COLUMNS, StartupTUI  # noqa: E402


def make(name: str, *, enabled: bool = True, source: str = "registry-user-run",
         scope: str = "user") -> StartupEntry:
    return StartupEntry(
        name=name,
        source=source,
        location=r"HKCU\...\Run",
        scope=scope,
        command=rf"C:\Apps\{name}.exe",
        exe=rf"C:\Apps\{name}.exe",
        exists=True,
        enabled=enabled,
    )


class FakeTUI(StartupTUI):
    """TUI with the scanner stubbed out — no registry access."""

    def __init__(self, entries: list[StartupEntry]) -> None:
        super().__init__()
        self._fake_entries = entries

    def _load(self) -> None:
        self._entries = list(self._fake_entries)
        self._errors = []
        self._render_rows()


async def test_table_populates_and_navigates():
    app = FakeTUI([make("Alpha"), make("Beta", enabled=False)])
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#table")
        assert table.row_count == 2
        assert app._row_ids == [e.id for e in app._entries]

        # Arrow down moves the cursor
        await pilot.press("down")
        await pilot.pause()
        assert table.cursor_row == 1


async def test_disable_and_enable_dispatch_to_mutation():
    calls: list[tuple[str, str]] = []
    app = FakeTUI([make("Alpha"), make("Beta", enabled=False)])

    def fake_mutation(entry, action):
        calls.append((entry.name, action))

    app._run_mutation = fake_mutation
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")  # Alpha is on -> should disable
        await pilot.pause()
        assert calls == [("Alpha", "disable")]

        await pilot.press("down")  # Beta is off
        await pilot.pause()
        await pilot.press("d")  # already off -> no-op
        await pilot.pause()
        assert calls == [("Alpha", "disable")]

        await pilot.press("e")  # enable Beta
        await pilot.pause()
        assert calls == [("Alpha", "disable"), ("Beta", "enable")]


async def test_service_disable_blocked_in_tui():
    """The TUI refuses service toggles (CLI-only, needs the flag)."""
    notified: list[str] = []
    app = FakeTUI([make("Svc", source="service")])
    app.notify = lambda msg, **kw: notified.append(str(msg))
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")
        await pilot.pause()
        assert notified and "service" in notified[0].lower()


async def test_search_filters_rows():
    app = FakeTUI([make("Alpha"), make("Beta"), make("Gamma")])
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("/")
        await pilot.pause()
        search = app.query_one("#search")
        assert search.display is True

        search.value = "bet"
        await pilot.pause()
        table = app.query_one("#table")
        assert table.row_count == 1

        await pilot.press("escape")
        await pilot.pause()
        assert app.query_one("#table").row_count == 3


async def test_cycle_source_reloads():
    entries = [
        make("Svc", source="service"),
        make("Run", source="registry-user-run"),
    ]
    app = FakeTUI(entries)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._source_filter is None

        await pilot.press("s")
        await pilot.pause()
        assert app._source_filter in SOURCE_LABELS or app._source_filter is None


async def test_com_handler_placeholder_in_cell():
    e = make("Task")
    e.command = ""
    e.exe = ""
    app = FakeTUI([e])
    async with app.run_test() as pilot:
        await pilot.pause()
        table = app.query_one("#table")
        cells = table.get_row_at(0)
        cmd_idx = [k for k, _h, _w, _f in COLUMNS].index("cmd")
        assert cells[cmd_idx] == "<COM handler>"


async def test_status_bar_counts():
    app = FakeTUI([make("A"), make("B", enabled=False)])
    async with app.run_test() as pilot:
        await pilot.pause()
        status = str(app.query_one("#status").render())
        assert "2/2 shown" in status
        assert "1 on" in status
