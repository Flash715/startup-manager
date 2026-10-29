"""Registry-based (Run / RunOnce) collectors."""

from __future__ import annotations

import os

from ..models import StartupEntry
from ..winapi import (
    IS_WINDOWS,
    expand_env,
    file_version_info,
    reg_read_binary,
    reg_read_values,
    split_command,
    winreg,
)

BASE = r"Software\Microsoft\Windows\CurrentVersion"
APPROVED = rf"{BASE}\Explorer\StartupApproved"

# (source id, hive name, key path tail, approved subkey name, scope)
_REG_TARGETS: list[tuple[str, str, str, str, str]] = [
    ("registry-user-run", "HKCU", rf"{BASE}\Run", r"Run", "user"),
    ("registry-user-runonce", "HKCU", rf"{BASE}\RunOnce", r"", "user"),
    ("registry-machine-run", "HKLM", rf"{BASE}\Run", r"Run", "machine"),
    ("registry-machine-runonce", "HKLM", rf"{BASE}\RunOnce", r"", "machine"),
    (
        "registry-machine-run-wow64",
        "HKLM",
        rf"{BASE}\Run",
        r"Run32",
        "machine",
    ),
    (
        "registry-machine-runonce-wow64",
        "HKLM",
        rf"{BASE}\RunOnce",
        r"",
        "machine",
    ),
]


def _hive(hive_name: str) -> int:
    if winreg is None:
        return 0
    return winreg.HKEY_CURRENT_USER if hive_name == "HKCU" else winreg.HKEY_LOCAL_MACHINE


def decode_approved(blob: bytes | None) -> tuple[bool, bool]:
    """Decode an Explorer StartupApproved blob -> (enabled, state_known)."""
    if blob is None or len(blob) < 1:
        return True, True  # untracked == enabled by default
    first = blob[0]
    if first in (0x02, 0x06):
        return True, True
    if first == 0x03:
        return False, True
    # Unknown marker: report as enabled but flag it.
    return True, False


class RegistryCollector:
    """Reads all Run/RunOnce keys, overlaying StartupApproved state."""

    def collect(self) -> list[StartupEntry]:
        if not IS_WINDOWS or winreg is None:
            return []
        entries: list[StartupEntry] = []
        for source, hive_name, path, approved_sub, scope in _REG_TARGETS:
            hive = _hive(hive_name)
            view = None
            if source.endswith("-wow64") and winreg is not None:
                view = winreg.KEY_WOW64_32KEY
            values = reg_read_values(hive, path, view=view or None)
            for name, data in values:
                exe, args = split_command(data)
                enabled, known = True, True
                if approved_sub:
                    blob = reg_read_binary(hive, rf"{APPROVED}\{approved_sub}", name)
                    enabled, known = decode_approved(blob)
                publisher, fver = file_version_info(exe)
                entries.append(
                    StartupEntry(
                        name=name,
                        source=source,
                        location=rf"{hive_name}\{path}",
                        scope=scope,  # type: ignore[arg-type]
                        command=expand_env(data) if "%" in data else data,
                        exe=exe,
                        args=args,
                        exists=bool(exe) and os.path.isfile(exe),
                        publisher=publisher,
                        file_version=fver,
                        enabled=enabled,
                        state_known=known,
                        deletable=True,
                        raw={"value": name},
                    )
                )
        return entries

    # -- mutations -------------------------------------------------------
    def set_enabled(self, entry: StartupEntry, enabled: bool) -> None:
        sub = {
            "registry-user-run": (winreg.HKEY_CURRENT_USER, r"Run"),
            "registry-machine-run": (winreg.HKEY_LOCAL_MACHINE, r"Run"),
            "registry-machine-run-wow64": (winreg.HKEY_LOCAL_MACHINE, r"Run32"),
        }.get(entry.source)
        if sub is None:
            raise ValueError(f"{entry.source} does not support enable/disable")
        hive, subkey = sub
        from .winapi import reg_write_binary

        reg_write_binary(
            hive,
            rf"{APPROVED}\{subkey}",
            entry.name,
            b"\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            if enabled
            else b"\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
        )

    def delete(self, entry: StartupEntry) -> None:

        hive_name = entry.location.split("\\", 1)[0]
        path = entry.location.split("\\", 1)[1]
        view_suffix = "-wow64" if entry.source.endswith("-wow64") else ""
        if not IS_WINDOWS or winreg is None:
            raise OSError("windows only")
        access = winreg.KEY_SET_VALUE
        access |= winreg.KEY_WOW64_32KEY if view_suffix else winreg.KEY_WOW64_64KEY
        key = winreg.OpenKey(_hive(hive_name), path, 0, access)
        try:
            winreg.DeleteValue(key, entry.name)
        finally:
            key.Close()
