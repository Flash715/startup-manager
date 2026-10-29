"""Backup, restore, quarantine and audit-log storage.

Layout (under %LOCALAPPDATA%\\startupmgr by default):
    backups/2026-10-30T01-02-03Z.json   full snapshots
    quarantine/                          deleted .lnk files
    audit.log                            append-only mutation log
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .models import StartupEntry


def data_dir() -> Path:
    base = os.environ.get("STARTUPMGR_HOME")
    if base:
        p = Path(base)
    else:
        local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        p = Path(local) / "startupmgr"
    p.mkdir(parents=True, exist_ok=True)
    return p


def backup_dir() -> Path:
    p = data_dir() / "backups"
    p.mkdir(parents=True, exist_ok=True)
    return p


def quarantine_dir() -> Path:
    p = data_dir() / "quarantine"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H-%M-%SZ")


# --------------------------------------------------------------------------
# Audit log
# --------------------------------------------------------------------------
def audit(action: str, entry: StartupEntry | None, detail: str = "") -> None:
    line = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "action": action,
        "id": entry.id if entry else "",
        "name": entry.name if entry else "",
        "source": entry.source if entry else "",
        "location": entry.location if entry else "",
        "detail": detail,
    }
    try:
        with open(data_dir() / "audit.log", "a", encoding="utf-8") as fh:
            fh.write(json.dumps(line, ensure_ascii=False) + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------------
# Snapshots
# --------------------------------------------------------------------------
def save_snapshot(entries: list[StartupEntry], label: str = "") -> Path:
    """Write a full JSON snapshot; returns the file path."""
    payload: dict[str, Any] = {
        "tool": "startupmgr",
        "version": 1,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "label": label,
        "count": len(entries),
        "entries": [e.to_dict() for e in entries],
    }
    name = f"{_now_stamp()}{'_' + label if label else ''}.json"
    path = backup_dir() / name
    counter = 1
    while path.exists():
        path = backup_dir() / f"{_now_stamp()}_{counter}{'_' + label if label else ''}.json"
        counter += 1
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    audit("snapshot", None, str(path))
    return path


def list_snapshots() -> list[Path]:
    files = sorted(backup_dir().glob("*.json"), key=lambda p: p.stat().st_mtime)
    return files


def latest_snapshot() -> Path | None:
    snaps = list_snapshots()
    return snaps[-1] if snaps else None


def load_snapshot(path: str | Path) -> list[StartupEntry]:
    with open(path, encoding="utf-8") as fh:
        payload = json.load(fh)
    entries = payload.get("entries") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{path} is not a startupmgr snapshot")
    return [StartupEntry.from_dict(d) for d in entries if isinstance(d, dict)]


# --------------------------------------------------------------------------
# Restore helpers per source
# --------------------------------------------------------------------------
class AlreadyRestored(Exception):
    """Raised when a snapshot entry is still present on the system."""


def _registry_value_exists(entry: StartupEntry) -> bool:
    from .winapi import IS_WINDOWS, winreg

    if not IS_WINDOWS or winreg is None:
        return True  # pretend present; nothing we can do off-Windows
    hive_name = entry.location.split("\\", 1)[0]
    path = entry.location.split("\\", 1)[1]
    hive = winreg.HKEY_CURRENT_USER if hive_name == "HKCU" else winreg.HKEY_LOCAL_MACHINE
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        try:
            winreg.QueryValueEx(key, entry.name)
        except OSError:
            continue
        else:
            return True
        finally:
            key.Close()
    return False


def _entry_present(entry: StartupEntry) -> bool:
    """Best-effort check whether the entry already exists on this machine."""
    src = entry.source
    if src.startswith("registry-"):
        return _registry_value_exists(entry)
    if src.startswith("startup-folder"):
        fname = os.path.basename(entry.raw.get("file", "")) or f"{entry.name}.lnk"
        return (Path(entry.location) / fname).exists()
    if src == "scheduled-task":
        import subprocess

        full = entry.location.rstrip("\\") + "\\" + entry.name
        res = subprocess.run(
            ["schtasks", "/query", "/tn", full],
            capture_output=True,
            text=True,
            timeout=15,
            creationflags=0x08000000,
            errors="replace",
        )
        return res.returncode == 0
    if src == "service":
        # Services are never deleted by this tool, so they are always "present".
        return True
    return False


def restore_entry(entry: StartupEntry) -> str:
    """Re-create one entry. Returns a short human status string.

    Raises ``AlreadyRestored`` when the entry is still present on the system, so
    callers can skip it instead of reporting a bogus failure.
    """
    src = entry.source
    present = _entry_present(entry)
    if present:
        raise AlreadyRestored(f"already present ({src})")

    if src.startswith("registry-"):
        from .winapi import IS_WINDOWS, reg_write_string, winreg

        if not IS_WINDOWS or winreg is None:
            return "skipped (not windows)"
        hive_name = entry.location.split("\\", 1)[0]
        path = entry.location.split("\\", 1)[1]
        hive = (
            winreg.HKEY_CURRENT_USER if hive_name == "HKCU" else winreg.HKEY_LOCAL_MACHINE
        )
        reg_write_string(hive, path, entry.name, entry.command)
        return "registry value restored"

    if src.startswith("startup-folder"):
        fname = os.path.basename(entry.raw.get("file", "")) or f"{entry.name}.lnk"
        src_file = quarantine_dir() / f"{entry.id}_{fname}"
        if not src_file.exists():
            # Fall back to any quarantined file with this id prefix.
            hits = list(quarantine_dir().glob(f"{entry.id}_*"))
            if hits:
                src_file = hits[0]
        if not src_file.exists():
            return "missing quarantine copy"
        dest = Path(entry.location) / fname
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src_file, dest)
        return f"restored {dest}"

    if src == "scheduled-task":
        xml = entry.raw.get("xml") or ""
        if not xml.strip():
            return "no task XML captured — cannot restore"
        import subprocess
        import tempfile

        tmp = Path(tempfile.gettempdir()) / f"startupmgr_{entry.id}.xml"
        tmp.write_text(xml, encoding="utf-16")
        full = entry.location.rstrip("\\") + "\\" + entry.name
        res = subprocess.run(
            ["schtasks", "/create", "/tn", full, "/xml", str(tmp), "/f"],
            capture_output=True,
            text=True,
            timeout=25,
            creationflags=0x08000000,
            errors="replace",
        )
        with contextlib.suppress(OSError):
            tmp.unlink()
        if res.returncode != 0:
            return f"failed: {(res.stderr or res.stdout).strip()[:160]}"
        return "scheduled task recreated"

    return f"restore not supported for {src}"
