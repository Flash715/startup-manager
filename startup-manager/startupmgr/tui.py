"""Interactive TUI built on Textual.

Optional dependency: ``uv sync --all-extras`` or ``pip install startupmgr[tui]``.
The plain CLI never imports this module, so the zero-dependency core stays intact.
"""

from __future__ import annotations

from typing import Any

from .models import SOURCE_LABELS, StartupEntry
from .scanner import Scanner, filter_entries
from .store import audit, save_snapshot

try:
    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.widgets import DataTable, Footer, Header, Input, Static
except ImportError as _exc:  # pragma: no cover - exercised only without textual
    raise ImportError(
        "The TUI needs the optional 'textual' package.\n"
        "  uv sync --all-extras          (in the project)\n"
        "  uv tool install . --extras tui --force   (global install)\n"
        "  pip install startupmgr[tui]"
    ) from _exc

_NO_WINDOW_MSG = (
    "elevation required — restart the terminal as Administrator "
    "to change machine-scope entries"
)

# Column key -> (header, width, value extractor)
COLUMNS: tuple[tuple[str, str, int, Any], ...] = (
    ("id", "ID", 10, lambda e: e.id),
    ("state", "STATE", 7, lambda e: "on" if e.enabled else "off"),
    ("source", "SOURCE", 20, lambda e: SOURCE_LABELS.get(e.source, e.source)),
    ("name", "NAME", 30, lambda e: e.name),
    ("cmd", "COMMAND", 45, lambda e: e.exe or e.command or "<COM handler>"),
    ("pub", "PUBLISHER", 24, lambda e: e.publisher or "-"),
)


class StartupTUI(App[None]):
    """Arrow-key table of autostart entries.

    Keys:
        ↑/↓  move            d  disable          e  enable
        s    cycle source    /  search           r  rescan
        q    quit
    """

    TITLE = "startupmgr"
    SUB_TITLE = "Windows autostart manager"

    CSS = """
    Screen { layout: vertical; }
    #search { height: 3; margin: 0 1; }
    #table { height: 1fr; margin: 0 1; }
    #status { height: 1; margin: 0 1; color: $text-muted; }
    #table DataTable { height: 100%; }
    DisabledRow { text-style: dim; }
    """

    BINDINGS = [
        Binding("q", "quit", "Quit"),
        Binding("d", "disable", "Disable"),
        Binding("e", "enable", "Enable"),
        Binding("s", "cycle_source", "Source"),
        Binding("r", "refresh", "Rescan"),
        Binding("/", "focus_search", "Search"),
        Binding("escape", "clear_search", "Clear search"),
    ]

    def __init__(self, source: str | None = None) -> None:
        super().__init__()
        self._source_filter: str | None = source
        self._entries: list[StartupEntry] = []
        self._errors: list[str] = []
        self._row_ids: list[str] = []

    # ---------------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        yield Input(placeholder="/  filter by name, path, publisher…", id="search")
        yield Vertical(DataTable(id="table", cursor_type="row"), id="tablewrap")
        yield Static("", id="status")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one("#table", DataTable)
        table.zebra_stripes = True
        for key, header, width, _fn in COLUMNS:
            table.add_column(header, key=key, width=width)
        self._load()
        search = self.query_one("#search", Input)
        search.display = False
        table.focus()

    # ---------------------------------------------------------------- data
    def _load(self) -> None:
        scanner = Scanner([self._source_filter] if self._source_filter else None)
        self._entries = scanner.scan()
        self._errors = list(scanner.errors)
        self._render_rows()

    def _render_rows(self) -> None:
        table = self.query_one("#table", DataTable)
        search = self.query_one("#search", Input)
        needle = search.value.strip().lower() if search.display else ""
        rows = filter_entries(self._entries, search=needle or None)

        table.clear()
        self._row_ids = []
        for e in rows:
            table.add_row(*(_fn(e) for _k, _h, _w, _fn in COLUMNS), key=e.id)
            self._row_ids.append(e.id)
        self._update_status(len(rows))

    def _update_status(self, shown: int) -> None:
        total = len(self._entries)
        on = sum(1 for e in self._entries if e.enabled)
        src = SOURCE_LABELS.get(self._source_filter or "", "all sources")
        missing = sum(
            1 for e in self._entries if not e.exists and (e.command or e.exe)
        )
        bits = [
            f"{shown}/{total} shown",
            f"{on} on · {total - on} off",
            f"source: {src}",
        ]
        if missing:
            bits.append(f"{missing} missing file")
        for err in self._errors[:2]:
            bits.append(f"warn: {err}")
        self.query_one("#status", Static).update("  ·  ".join(bits))

    def _current(self) -> StartupEntry | None:
        table = self.query_one("#table", DataTable)
        row_key = table.cursor_row
        if 0 <= row_key < len(self._row_ids):
            target = self._row_ids[row_key]
            for e in self._entries:
                if e.id == target:
                    return e
        return None

    def _run_mutation(self, entry: StartupEntry, action: str) -> None:
        from .cli import _collector_for

        coll = _collector_for(entry)

        if entry.scope == "machine":
            from .winapi import is_admin

            if not is_admin():
                self.notify(_NO_WINDOW_MSG, severity="error", timeout=6)
                return

        save_snapshot(self._entries, label=f"pre-{action}")
        try:
            if action == "enable":
                coll.set_enabled(entry, True)
            else:
                coll.set_enabled(entry, False)
        except PermissionError as exc:
            self.notify(str(exc)[:200], severity="error", timeout=6)
            return
        except Exception as exc:  # noqa: BLE001
            self.notify(f"{action} failed: {exc}", severity="error", timeout=6)
            return

        audit(action, entry)
        entry.enabled = action == "enable"
        self._render_rows()
        self.notify(
            f"{entry.name} {'enabled' if action == 'enable' else 'disabled'}",
            severity="information",
            timeout=3,
        )

    # ---------------------------------------------------------------- actions
    def action_disable(self) -> None:
        e = self._current()
        if e is None:
            return
        if e.source == "service":
            self.notify(
                "services can only be disabled from the CLI with "
                "--i-know-what-im-doing",
                severity="warning",
                timeout=5,
            )
            return
        if not e.enabled:
            return
        self._run_mutation(e, "disable")

    def action_enable(self) -> None:
        e = self._current()
        if e is None or e.enabled:
            return
        if e.source.endswith("runonce"):
            self.notify("RunOnce entries have no state — delete instead",
                        severity="warning", timeout=5)
            return
        self._run_mutation(e, "enable")

    def action_cycle_source(self) -> None:
        order: list[str | None] = [None, *sorted(SOURCE_LABELS)]
        idx = order.index(self._source_filter)
        self._source_filter = order[(idx + 1) % len(order)]
        self._load()

    def action_refresh(self) -> None:
        self._load()

    def action_focus_search(self) -> None:
        search = self.query_one("#search", Input)
        search.display = True
        search.focus()

    def action_clear_search(self) -> None:
        search = self.query_one("#search", Input)
        search.value = ""
        search.display = False
        self.query_one("#table", DataTable).focus()
        self._render_rows()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "search":
            self._render_rows()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "search":
            self.set_focus(None)
            self.query_one("#table", DataTable).focus()


def run_tui(source: str | None = None) -> int:
    """Entry point for `startupmgr tui`."""
    StartupTUI(source=source).run()
    return 0
