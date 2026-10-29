"""Command-line interface for startupmgr."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from . import __version__
from .models import ALL_SOURCES, SOURCE_LABELS, StartupEntry
from .render import (
    bold,
    cyan,
    dim,
    green,
    red,
    render_csv,
    render_json,
    render_jsonl,
    render_summary,
    render_table,
    yellow,
)
from .scanner import Scanner, filter_entries
from .store import (
    AlreadyRestored,
    audit,
    latest_snapshot,
    list_snapshots,
    load_snapshot,
    restore_entry,
    save_snapshot,
)
from .winapi import IS_WINDOWS, is_admin

# Exit codes (documented in the PRD)
EX_OK = 0
EX_ERROR = 1
EX_USAGE = 2
EX_NOT_FOUND = 3
EX_NEEDS_ADMIN = 4
EX_REFUSED = 5
EX_UNSUPPORTED = 6


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _need_windows() -> None:
    if not IS_WINDOWS:
        print(red("error: startupmgr only runs on Windows."), file=sys.stderr)
        raise SystemExit(EX_UNSUPPORTED)


def _scan(args: argparse.Namespace) -> tuple[list[StartupEntry], list[str]]:
    sources = getattr(args, "source", None) or ALL_SOURCES
    scanner = Scanner(sources)
    entries = scanner.scan()
    entries = filter_entries(
        entries,
        sources=None,  # already filtered by the scanner
        scope=getattr(args, "scope", None),
        search=getattr(args, "search", None),
        enabled_only=getattr(args, "enabled_only", False),
        disabled_only=getattr(args, "disabled_only", False),
        missing_only=getattr(args, "missing_only", False),
    )
    return entries, scanner.errors


def _emit(entries: list[StartupEntry], args: argparse.Namespace, errors: list[str]) -> None:
    fmt = getattr(args, "format", "table")
    if fmt == "json":
        print(render_json(entries))
        return
    if fmt == "jsonl":
        print(render_jsonl(entries))
        return
    if fmt == "csv":
        print(render_csv(entries))
        return

    print(render_table(entries))
    if not getattr(args, "no_summary", False):
        print(render_summary(entries))
    for err in errors:
        print(yellow(f"  warn: {err}"), file=sys.stderr)


def _resolve(entries: list[StartupEntry], target: str) -> StartupEntry | None:
    """Find an entry by id, by exact name, or by unique name substring."""
    t = target.strip().lower()
    for e in entries:
        if e.id == t:
            return e
    exact = [e for e in entries if e.name.lower() == t]
    if len(exact) == 1:
        return exact[0]
    partial = [e for e in entries if t in e.name.lower()]
    if len(partial) == 1:
        return partial[0]
    if len(partial) > 1:
        print(red(f"error: '{target}' matches {len(partial)} entries:"), file=sys.stderr)
        for e in partial[:15]:
            print(
                f"   {e.id}  {e.name}  ({SOURCE_LABELS.get(e.source, e.source)})",
                file=sys.stderr,
            )
        print(dim("  specify the id instead."), file=sys.stderr)
    return None


def _collector_for(entry: StartupEntry):
    from .collectors import (
        FolderCollector,
        RegistryCollector,
        ServiceCollector,
        TaskCollector,
    )

    if entry.source.startswith("registry-"):
        return RegistryCollector()
    if entry.source.startswith("startup-folder"):
        return FolderCollector()
    if entry.source == "scheduled-task":
        return TaskCollector()
    if entry.source == "service":
        return ServiceCollector()
    raise ValueError(entry.source)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------
def cmd_list(args: argparse.Namespace) -> int:
    _need_windows()
    entries, errors = _scan(args)
    _emit(entries, args, errors)
    return EX_OK


def cmd_sources(_args: argparse.Namespace) -> int:
    print(bold("Available sources:\n"))
    for sid in ALL_SOURCES:
        print(f"  {cyan(sid):<34} {dim(SOURCE_LABELS[sid])}")
    return EX_OK


def cmd_show(args: argparse.Namespace) -> int:
    _need_windows()
    entries, errors = _scan(args)
    if not entries:
        for err in errors:
            print(yellow(f"warn: {err}"), file=sys.stderr)
    entry = _resolve(entries, args.target)
    if entry is None:
        print(red(f"error: no startup entry matching '{args.target}'"), file=sys.stderr)
        return EX_NOT_FOUND

    d = entry.to_dict()
    width = max(len(k) for k in d)
    print(bold(f"\n  {entry.name}\n"))
    for k, v in d.items():
        label = dim(k.ljust(width))
        if k == "enabled":
            v = green("true") if v else red("false")
        elif k == "exists":
            v = green("true") if v else yellow("false")
        print(f"  {label}  {v}")
    print()
    return EX_OK


def _mutate(args: argparse.Namespace, action: str) -> int:
    _need_windows()
    entries, _errors = _scan(args)
    targets: list[StartupEntry] = []
    for t in args.targets:
        e = _resolve(entries, t)
        if e is None:
            print(red(f"error: no startup entry matching '{t}'"), file=sys.stderr)
            return EX_NOT_FOUND
        targets.append(e)

    # Refuse impossible operations *before* asking for elevation.
    if action == "delete":
        bad = [e for e in targets if e.source == "service"]
        if bad:
            for e in bad:
                print(
                    red(f"refused: cannot delete service {e.name}.")
                    + "\n"
                    + dim(f"  use: startupmgr disable {e.id} --i-know-what-im-doing"),
                    file=sys.stderr,
                )
            return EX_REFUSED

    if action == "disable" and not getattr(args, "i_know_what_im_doing", False):
        svc = [e for e in targets if e.source == "service"]
        if svc:
            for e in svc:
                print(
                    red(f"refused: {e.name} is a Windows service.")
                    + "\n"
                    + dim("  add --i-know-what-im-doing to disable a service."),
                    file=sys.stderr,
                )
            return EX_REFUSED

    machine_targets = [e for e in targets if e.scope == "machine"]
    if machine_targets and not is_admin() and action != "snapshot":
        names = ", ".join(e.name for e in machine_targets)
        print(
            red(f"error: elevation required for machine-scope entries ({names}).")
            + "\n"
            + dim("  re-run this command from an Administrator terminal."),
            file=sys.stderr,
        )
        return EX_NEEDS_ADMIN

    # Snapshot before any destructive action.
    pre = None
    if action in ("delete", "disable", "enable") and not args.dry_run:
        pre = save_snapshot(entries, label=f"pre-{action}")

    rc = EX_OK
    for e in targets:
        coll = _collector_for(e)
        try:
            if action == "enable":
                coll.set_enabled(e, True)
                audit("enable", e)
                print(green("✓ enabled ") + f"{e.name}  {dim(e.id)}")
            elif action == "disable":
                if args.dry_run:
                    print(dim(f"[dry-run] would disable {e.name} ({e.id})"))
                    continue
                coll.set_enabled(e, False)
                audit("disable", e)
                print(red("✗ disabled ") + f"{e.name}  {dim(e.id)}")
            elif action == "delete":
                if args.dry_run:
                    print(
                        dim(f"[dry-run] would delete {e.name} ({e.id}) from {e.location}")
                    )
                    continue
                coll.delete(e)
                audit("delete", e, f"backup={pre}" if pre else "")
                print(red("✗ deleted ") + f"{e.name}  {dim(e.id)}")
        except PermissionError as exc:
            print(red(f"error: {e.name}: {exc}"), file=sys.stderr)
            rc = EX_NEEDS_ADMIN
        except FileNotFoundError as exc:
            print(red(f"error: {e.name}: file not found ({exc})"), file=sys.stderr)
            rc = EX_ERROR
        except Exception as exc:  # noqa: BLE001
            print(red(f"error: {e.name}: {exc}"), file=sys.stderr)
            rc = EX_ERROR

    if pre and not args.dry_run:
        print(dim(f"\n  backup written: {pre}"))
        print(dim(f"  undo with: startupmgr restore {pre}"))
    return rc


def cmd_enable(args: argparse.Namespace) -> int:
    return _mutate(args, "enable")


def cmd_disable(args: argparse.Namespace) -> int:
    return _mutate(args, "disable")


def cmd_delete(args: argparse.Namespace) -> int:
    return _mutate(args, "delete")


def cmd_backup(args: argparse.Namespace) -> int:
    _need_windows()
    entries, _errors = _scan(args)
    path = save_snapshot(entries, label=args.label or "")
    print(green("✓ backup written ") + str(path))
    print(dim(f"  {len(entries)} entries captured"))
    return EX_OK


def cmd_backups(_args: argparse.Namespace) -> int:
    snaps = list_snapshots()
    if not snaps:
        print(dim("  (no backups yet)"))
        return EX_OK
    print(bold("  Backups:\n"))
    for p in snaps:
        try:
            size = p.stat().st_size
        except OSError:
            size = 0
        count = ""
        try:
            import json

            with open(p, encoding="utf-8") as fh:
                data = json.load(fh)
            count = f"  {data.get('count', '?')} entries"
        except Exception:
            pass
        print(f"  {cyan(p.name)}  {dim(f'{size / 1024:.1f} KiB{count}')}")
    return EX_OK


def cmd_restore(args: argparse.Namespace) -> int:
    _need_windows()
    if args.file == "latest":
        path = latest_snapshot()
        if path is None:
            print(red("error: no backups found."), file=sys.stderr)
            return EX_NOT_FOUND
    else:
        path = args.file
    try:
        entries = load_snapshot(path)
    except Exception as exc:  # noqa: BLE001
        print(red(f"error: cannot read {path}: {exc}"), file=sys.stderr)
        return EX_ERROR

    print(bold(f"  Restoring from {path}  ({len(entries)} entries)\n"))
    rc = EX_OK
    restored = skipped = 0
    for e in entries:
        if args.dry_run:
            print(dim(f"[dry-run] would restore {e.name} ({e.source})"))
            continue
        try:
            status = restore_entry(e)
        except AlreadyRestored:
            skipped += 1
            continue
        except PermissionError as exc:
            status = f"permission denied: {exc}"
            rc = EX_NEEDS_ADMIN
        except Exception as exc:  # noqa: BLE001
            status = f"error: {exc}"
            rc = EX_ERROR
        ok = not status.lower().startswith(
            ("error", "fail", "missing", "no task", "permission", "restore not")
        )
        if ok:
            restored += 1
        else:
            rc = rc or EX_ERROR
        mark = green("✓") if ok else yellow("!")
        print(f"  {mark} {e.name:<32} {dim(status)}")
        audit("restore", e, status)

    if not args.dry_run:
        print()
        print(
            green(f"  {restored} restored")
            + dim(f" · {skipped} already present · {len(entries)} in snapshot")
        )
    return rc


def cmd_audit(args: argparse.Namespace) -> int:
    from .store import data_dir

    log = data_dir() / "audit.log"
    if not log.exists():
        print(dim("  (audit log is empty)"))
        return EX_OK
    lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
    tail = lines[-args.lines :] if args.lines else lines
    print(bold(f"  Audit log: {log}\n"))
    for line in tail:
        print("  " + line)
    return EX_OK


def cmd_doctor(_args: argparse.Namespace) -> int:
    print(bold("\n  startupmgr doctor\n"))
    row = lambda k, v: print(f"  {dim(k.ljust(22))} {v}")  # noqa: E731
    row("version", __version__)
    row("platform", sys.platform)
    row("python", sys.version.split()[0])
    row("windows", green("yes") if IS_WINDOWS else red("no"))
    row("elevated", green("yes") if is_admin() else yellow("no (read-only mode)"))
    from .store import data_dir

    row("store", str(data_dir()))
    snaps = list_snapshots()
    row("backups", str(len(snaps)))
    if IS_WINDOWS:
        print()
        entries, errors = _scan(
            argparse.Namespace(
                source=None,
                scope=None,
                search=None,
                enabled_only=False,
                disabled_only=False,
                missing_only=False,
            )
        )
        row("entries found", str(len(entries)))
        if errors:
            for err in errors:
                print(yellow(f"  warn: {err}"))
    print()
    return EX_OK


# --------------------------------------------------------------------------
# Parser
# --------------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="startupmgr",
        description="List, disable and remove Windows startup programs.",
        epilog="Run `startupmgr <command> -h` for command-specific help.",
    )
    p.add_argument("--version", action="version", version=f"startupmgr {__version__}")

    sub = p.add_subparsers(dest="command", required=True, metavar="<command>")

    def add_filters(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "-s",
            "--source",
            action="append",
            choices=ALL_SOURCES,
            help="limit to a source (repeatable)",
        )
        sp.add_argument("--scope", choices=("user", "machine"))
        sp.add_argument("-q", "--search", help="substring match on name/path/publisher")
        g = sp.add_mutually_exclusive_group()
        g.add_argument("--enabled-only", action="store_true")
        g.add_argument("--disabled-only", action="store_true")
        sp.add_argument(
            "--missing-only",
            action="store_true",
            help="only entries whose target file is gone",
        )

    def add_format(sp: argparse.ArgumentParser) -> None:
        sp.add_argument(
            "-f",
            "--format",
            choices=("table", "json", "jsonl", "csv"),
            default="table",
        )
        sp.add_argument("--no-summary", action="store_true")

    # list
    sp = sub.add_parser("list", aliases=["ls"], help="list startup entries")
    add_filters(sp)
    add_format(sp)
    sp.set_defaults(func=cmd_list)

    # sources
    sp = sub.add_parser("sources", help="show all supported source ids")
    sp.set_defaults(func=cmd_sources)

    # show
    sp = sub.add_parser("show", help="show full details for one entry")
    add_filters(sp)
    sp.add_argument("target", help="entry id or name")
    sp.set_defaults(func=cmd_show)

    # enable / disable / delete
    for name, fn, hlp in (
        ("enable", cmd_enable, "re-enable a disabled entry (non-destructive)"),
        ("disable", cmd_disable, "disable an entry without deleting it"),
        ("delete", cmd_delete, "permanently remove an entry (auto-backup first)"),
    ):
        sp = sub.add_parser(name, help=hlp)
        add_filters(sp)
        sp.add_argument("targets", nargs="+", help="entry id(s) or name(s)")
        sp.add_argument(
            "-n",
            "--dry-run",
            action="store_true",
            help="show what would happen, change nothing",
        )
        if name == "disable":
            sp.add_argument(
                "--i-know-what-im-doing",
                action="store_true",
                help="required to disable Windows services",
            )
        sp.set_defaults(func=fn)

    # backup / backups / restore
    sp = sub.add_parser("backup", help="write a snapshot of all entries")
    sp.add_argument("-l", "--label", help="label to add to the filename")
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("backups", help="list snapshots")
    sp.set_defaults(func=cmd_backups)

    sp = sub.add_parser("restore", help="restore entries from a snapshot")
    sp.add_argument("file", help="snapshot path, or 'latest'")
    sp.add_argument("-n", "--dry-run", action="store_true")
    sp.set_defaults(func=cmd_restore)

    # audit / doctor
    sp = sub.add_parser("audit", help="show the mutation audit log")
    sp.add_argument("-n", "--lines", type=int, default=50, help="tail N lines")
    sp.set_defaults(func=cmd_audit)

    sp = sub.add_parser("doctor", help="diagnose environment and sources")
    sp.set_defaults(func=cmd_doctor)

    # tui
    def cmd_tui(a: argparse.Namespace) -> int:
        try:
            from .tui import run_tui
        except ImportError as exc:
            print(red(str(exc)), file=sys.stderr)
            return EX_UNSUPPORTED
        return run_tui(a.source)

    sp = sub.add_parser(
        "tui", help="interactive table — arrow keys, toggle enable/disable"
    )
    sp.add_argument(
        "-s", "--source", choices=ALL_SOURCES,
        help="start filtered to one source",
    )
    sp.set_defaults(func=cmd_tui)

    return p


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
