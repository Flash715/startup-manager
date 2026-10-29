"""Terminal output: colour handling, table / json / csv rendering."""

from __future__ import annotations

import csv
import io
import json
import os
import shutil
import sys
import unicodedata
from collections.abc import Iterable

from .models import SOURCE_LABELS, StartupEntry

_TTY = sys.stdout.isatty()
NO_COLOR = bool(os.environ.get("NO_COLOR")) or not _TTY


def c(text: str, code: str, *, force: bool = False) -> str:
    if NO_COLOR and not force:
        return text
    return f"\x1b[{code}m{text}\x1b[0m"


def green(t: str) -> str:
    return c(t, "32")


def red(t: str) -> str:
    return c(t, "31")


def yellow(t: str) -> str:
    return c(t, "33")


def cyan(t: str) -> str:
    return c(t, "36")


def dim(t: str) -> str:
    return c(t, "2")


def bold(t: str) -> str:
    return c(t, "1")


def _w(text: str) -> int:
    """Display width, counting CJK/emoji as 2 columns."""
    total = 0
    for ch in text:
        if unicodedata.combining(ch):
            continue
        ea = unicodedata.east_asian_width(ch)
        total += 2 if ea in ("W", "F") else 1
    return total


def _pad(text: str, width: int) -> str:
    pad = width - _w(text)
    return text + " " * max(0, pad)


def truncate(text: str, width: int) -> str:
    if _w(text) <= width:
        return text
    out, acc = "", 0
    for ch in text:
        w = 2 if unicodedata.east_asian_width(ch) in ("W", "F") else 1
        if acc + w > width - 1:
            break
        out += ch
        acc += w
    return out + "…"


# --------------------------------------------------------------------------
# Table
# --------------------------------------------------------------------------
COLS = ("ID", "STATE", "SOURCE", "NAME", "COMMAND", "PUBLISHER")


COM_PLACEHOLDER = "<COM handler>"


def _row_values(e: StartupEntry, cmd_width: int) -> tuple[str, str, str, str, str, str]:
    state = green("on") if e.enabled else red("off")
    if not e.enabled and not e.state_known:
        state = yellow("?")
    source = SOURCE_LABELS.get(e.source, e.source)
    if not e.command and not e.exe:
        return (e.id, state, source, e.name, dim(COM_PLACEHOLDER), e.publisher or "-")
    cmd = e.exe if e.exists else (e.exe or e.command)
    if not e.exists:
        cmd = truncate(cmd, cmd_width)
    return (e.id, state, source, e.name, cmd, e.publisher or "-")


def render_table(entries: list[StartupEntry], *, show_missing: bool = True) -> str:
    if not entries:
        return dim("  (no matching startup entries)")

    term_w = shutil.get_terminal_size((140, 24)).columns
    fixed = 10 + 5 + 22 + 26 + 30 + 4  # rough width of all non-command columns
    cmd_width = max(20, min(60, term_w - fixed))

    header = tuple(COLS)
    rows: list[tuple[str, ...]] = [header]
    plain: list[tuple[str, ...]] = []

    for e in entries:
        rows.append(_row_values(e, cmd_width))
        raw_cmd = e.exe or e.command
        plain.append(
            (
                e.id,
                "on" if e.enabled else "off",
                SOURCE_LABELS.get(e.source, e.source),
                e.name,
                truncate(raw_cmd, cmd_width) if raw_cmd else COM_PLACEHOLDER,
                e.publisher or "-",
            )
        )

    widths = [0] * len(COLS)
    for r in plain:
        for i, v in enumerate(r):
            widths[i] = max(widths[i], _w(v))
    widths[3] = min(widths[3], 28)
    widths[5] = min(widths[5], 24)

    out: list[str] = []
    head = "  ".join(bold(_pad(h, widths[i])) for i, h in enumerate(header))
    out.append("  " + head)
    out.append("  " + dim("─" * min(term_w - 2, sum(widths) + 2 * (len(widths) - 1))))

    for idx, e in enumerate(entries):
        vals = rows[idx + 1]
        cells = []
        for i, v in enumerate(vals):
            if i == 1:  # already coloured, pad manually
                cells.append(_pad(v, widths[i]))
            else:
                cells.append(_pad(truncate(v, widths[i]), widths[i]))
        line = "  ".join(cells)
        if not e.enabled:
            line = dim(line)
        if not e.exists and show_missing and (e.command or e.exe):
            line += " " + yellow("⚠ missing file")
        out.append("  " + line)

    return "\n".join(out)


# --------------------------------------------------------------------------
# Summary
# --------------------------------------------------------------------------
def render_summary(entries: list[StartupEntry]) -> str:
    total = len(entries)
    on = sum(1 for e in entries if e.enabled)
    missing = sum(1 for e in entries if not e.exists and (e.command or e.exe))
    by_source: dict[str, int] = {}
    for e in entries:
        by_source[e.source] = by_source.get(e.source, 0) + 1

    lines = [
        "",
        bold(f"  {total} entries")
        + dim(" · ")
        + green(f"{on} enabled")
        + dim(" · ")
        + red(f"{total - on} disabled")
        + (dim(" · ") + yellow(f"{missing} missing file") if missing else ""),
    ]
    if by_source:
        parts = [
            f"{SOURCE_LABELS.get(s, s)}: {n}"
            for s, n in sorted(by_source.items(), key=lambda kv: -kv[1])
        ]
        lines.append(dim("  " + "  |  ".join(parts)))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# JSON / CSV
# --------------------------------------------------------------------------
def render_json(entries: Iterable[StartupEntry]) -> str:
    return json.dumps(
        {
            "count": sum(1 for _ in entries)
            if not isinstance(entries, list)
            else len(entries),
            "entries": [e.to_dict() for e in entries],
        },
        indent=2,
        ensure_ascii=False,
    )


def render_jsonl(entries: Iterable[StartupEntry]) -> str:
    return "\n".join(e.to_json() for e in entries)


CSV_FIELDS = [
    "id",
    "name",
    "source",
    "scope",
    "enabled",
    "location",
    "command",
    "exe",
    "args",
    "exists",
    "publisher",
    "file_version",
]


def render_csv(entries: Iterable[StartupEntry]) -> str:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    for e in entries:
        writer.writerow(e.to_dict())
    return buf.getvalue().rstrip()
