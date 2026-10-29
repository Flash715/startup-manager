"""Startup-folder (.lnk) collectors."""

from __future__ import annotations

import os
import shutil
import struct

from ..models import StartupEntry
from ..winapi import IS_WINDOWS, file_version_info, reg_read_binary, winreg

FOLDER_APPROVED = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer\StartupApproved\StartupFolder"
)
FOLDER_APPROVED_32 = (
    r"Software\Microsoft\Windows\CurrentVersion\Explorer"
    r"\StartupApproved\StartupFolder32"
)


def _startup_dirs() -> list[tuple[str, str, str]]:
    """Return [(source, path, scope)] for both Startup folders."""
    appdata = os.environ.get("APPDATA", "")
    programdata = os.environ.get("PROGRAMDATA", "")
    out: list[tuple[str, str, str]] = []
    if appdata:
        out.append(
            (
                "startup-folder-user",
                os.path.join(
                    appdata,
                    "Microsoft",
                    "Windows",
                    "Start Menu",
                    "Programs",
                    "Startup",
                ),
                "user",
            )
        )
    if programdata:
        out.append(
            (
                "startup-folder-common",
                os.path.join(
                    programdata,
                    "Microsoft",
                    "Windows",
                    "Start Menu",
                    "Programs",
                    "Startup",
                ),
                "machine",
            )
        )
    return out


def resolve_lnk_target(lnk_path: str) -> str:
    """Resolve a .lnk to its target path.

    Primary: WScript.Shell via PowerShell (reliable, no COM bindings needed).
    Fallback: raw binary scan for the target path embedded in the LinkInfo block.
    """
    if not os.path.isfile(lnk_path):
        return ""

    if IS_WINDOWS:
        try:
            import subprocess

            ps = (
                "$s=(New-Object -ComObject WScript.Shell)"
                f".CreateShortcut('{lnk_path.replace(chr(39), chr(39) * 2)}');"
                "Write-Output $s.TargetPath"
            )
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True,
                text=True,
                timeout=8,
                creationflags=0x08000000,  # CREATE_NO_WINDOW
            )
            target = res.stdout.strip()
            if target and os.path.exists(target):
                return target
            if target:
                return target
        except Exception:
            pass

    # Fallback: parse the link by scanning for an absolute path string.
    try:
        with open(lnk_path, "rb") as fh:
            data = fh.read()
        # Look for both ANSI and UTF-16LE absolute paths ending in a known ext.
        for enc in ("ascii", "utf-16-le"):
            for m in _iter_abs_paths(data, enc):
                candidate = m
                if any(
                    candidate.lower().endswith(ext)
                    for ext in (".exe", ".com", ".bat", ".cmd", ".ps1", ".vbs")
                ):
                    return candidate
        _ = struct  # keep import used for future LinkInfo parsing
    except Exception:
        pass
    return ""


def _iter_abs_paths(data: bytes, enc: str) -> list[str]:
    """Very rough scanner for drive-letter paths inside binary blobs."""
    out: list[str] = []
    step = len("C:\\".encode(enc))
    marker = "C:\\".encode(enc)
    idx = 0
    while True:
        idx = data.find(marker, idx)
        if idx < 0:
            break
        end = idx
        # walk forward until a NUL pair or a non-printable junction
        while end < len(data) - step:
            chunk = data[end : end + step]
            if enc == "utf-16-le":
                if chunk[1:2] == b"\x00" and 32 <= chunk[0] < 127:
                    end += 2
                    continue
                break
            if 32 <= chunk[0] < 127:
                end += 1
                continue
            break
        try:
            text = data[idx:end].decode(enc, "ignore")
            if len(text) > 3:
                out.append(text)
        except Exception:
            pass
        idx = max(end, idx + 1)
    return out


class FolderCollector:
    """Enumerates .lnk and .url files in both Startup folders."""

    def collect(self) -> list[StartupEntry]:
        if not IS_WINDOWS:
            return []
        entries: list[StartupEntry] = []
        for source, folder, scope in _startup_dirs():
            if not os.path.isdir(folder):
                continue
            try:
                names = sorted(os.listdir(folder))
            except OSError:
                continue
            for fname in names:
                if fname.lower() == "desktop.ini":
                    continue
                full = os.path.join(folder, fname)
                if not os.path.isfile(full):
                    continue
                stem = os.path.splitext(fname)[0]
                target = ""
                if fname.lower().endswith(".lnk"):
                    target = resolve_lnk_target(full)
                elif fname.lower().endswith(".url"):
                    target = _read_url_target(full)
                else:
                    target = full

                enabled, known = True, True
                blob = (
                    reg_read_binary(
                        winreg.HKEY_CURRENT_USER if winreg else 0,
                        FOLDER_APPROVED,
                        fname,
                    )
                    if winreg
                    else None
                )
                if blob is None and scope == "machine" and winreg:
                    blob = reg_read_binary(
                        winreg.HKEY_LOCAL_MACHINE, FOLDER_APPROVED, fname
                    )
                if blob is not None:
                    from .registry import decode_approved

                    enabled, known = decode_approved(blob)

                publisher, fver = (
                    file_version_info(target)
                    if target.lower().endswith((".exe", ".com"))
                    else ("", "")
                )
                exists = bool(target) and os.path.exists(target)
                entries.append(
                    StartupEntry(
                        name=stem,
                        source=source,
                        location=folder,
                        scope=scope,  # type: ignore[arg-type]
                        command=target or full,
                        exe=target,
                        args="",
                        exists=exists,
                        publisher=publisher,
                        file_version=fver,
                        enabled=enabled,
                        state_known=known,
                        deletable=True,
                        delete_block_reason="" if exists else "target missing",
                        raw={"file": full},
                    )
                )
        return entries

    # -- mutations -------------------------------------------------------
    def set_enabled(self, entry: StartupEntry, enabled: bool) -> None:
        from ..winapi import reg_write_binary

        fname = os.path.basename(entry.raw.get("file", "")) or entry.name
        hive = (
            winreg.HKEY_CURRENT_USER if entry.scope == "user" else winreg.HKEY_LOCAL_MACHINE
        )
        reg_write_binary(
            hive,
            FOLDER_APPROVED,
            fname,
            b"\x02\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00"
            if enabled
            else b"\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00",
        )

    def delete(self, entry: StartupEntry) -> None:
        path = entry.raw.get("file", "")
        if not path or not os.path.isfile(path):
            raise FileNotFoundError(path or "<unknown>")
        # Copy to a quarantine dir instead of hard-deleting, so restore is trivial.
        from ..store import quarantine_dir

        qdir = quarantine_dir()
        dest = os.path.join(qdir, f"{entry.id}_{os.path.basename(path)}")
        shutil.copy2(path, dest)
        os.remove(path)


def _read_url_target(path: str) -> str:
    try:
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                if line.upper().startswith("URL="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""
