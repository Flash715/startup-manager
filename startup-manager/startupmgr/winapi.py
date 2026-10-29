"""Thin, defensive wrappers over the Windows API bits we need.

Everything here must degrade gracefully: never raise AccessDenied up the stack,
return empty/false instead.
"""

from __future__ import annotations

import ctypes
import os
import re
import sys
from ctypes import wintypes

IS_WINDOWS = sys.platform == "win32"

if IS_WINDOWS:
    import winreg
else:  # pragma: no cover - allows importing the package on Linux for tests
    winreg = None  # type: ignore[assignment]


# --------------------------------------------------------------------------
# Elevation
# --------------------------------------------------------------------------
def is_admin() -> bool:
    """True if the current process is elevated."""
    if not IS_WINDOWS:
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


# --------------------------------------------------------------------------
# Environment / path expansion
# --------------------------------------------------------------------------
_ENV_RE = re.compile(r"%([^%]+)%")


def expand_env(text: str) -> str:
    """Expand %VAR% style environment variables case-insensitively."""

    def repl(m: re.Match[str]) -> str:
        return os.environ.get(m.group(1), m.group(0))

    if not text:
        return ""
    return _ENV_RE.sub(repl, text)


_QUOTED_RE = re.compile(r'^"([^"]+)"\s*(.*)$')
_EXE_HINT = re.compile(
    r"^(.+?\.(?:exe|com|bat|cmd|ps1|vbs|js|lnk|msc|cpl|scr))\b(.*)$",
    re.IGNORECASE,
)


def split_command(command: str) -> tuple[str, str]:
    """Split a command line into (exe_path, args).

    Handles quoted paths, unquoted paths with spaces before ``.exe``, and
    bare folder/lnk references.
    """
    cmd = command.strip()
    if not cmd:
        return "", ""

    # Windows drops a leading "start " for Run entries pointing to docs.
    if cmd.lower().startswith("start "):
        cmd = cmd[6:].lstrip('"').rstrip()

    m = _QUOTED_RE.match(cmd)
    if m:
        return expand_env(m.group(1)), m.group(2).strip()

    m = _EXE_HINT.match(cmd)
    if m:
        return expand_env(m.group(1)), m.group(2).strip()

    # No extension found: first token only.
    parts = cmd.split(None, 1)
    return expand_env(parts[0]), (parts[1].strip() if len(parts) > 1 else "")


# --------------------------------------------------------------------------
# PE version resource -> CompanyName / FileVersion
# --------------------------------------------------------------------------
class _VS_FIXEDFILEINFO(ctypes.Structure):
    _fields_ = [
        ("dwSignature", wintypes.DWORD),
        ("dwStrucVersion", wintypes.DWORD),
        ("dwFileVersionMS", wintypes.DWORD),
        ("dwFileVersionLS", wintypes.DWORD),
        ("dwProductVersionMS", wintypes.DWORD),
        ("dwProductVersionLS", wintypes.DWORD),
        ("dwFileFlagsMask", wintypes.DWORD),
        ("dwFileFlags", wintypes.DWORD),
        ("dwFileOS", wintypes.DWORD),
        ("dwFileType", wintypes.DWORD),
        ("dwFileSubtype", wintypes.DWORD),
        ("dwFileDateMS", wintypes.DWORD),
        ("dwFileDateLS", wintypes.DWORD),
    ]


def file_version_info(path: str) -> tuple[str, str]:
    """Return (publisher, file_version) for a PE file. Best effort."""
    if not IS_WINDOWS or not path or not os.path.isfile(path):
        return "", ""
    try:
        size = ctypes.windll.version.GetFileVersionInfoSizeW(path, None)
        if size == 0:
            return "", ""
        buf = ctypes.create_string_buffer(size)
        if not ctypes.windll.version.GetFileVersionInfoW(path, 0, size, buf):
            return "", ""

        ver = ""

        # --- Fixed info for a numeric version fallback
        ptr = ctypes.c_void_p()
        length = wintypes.UINT()
        if (
            ctypes.windll.version.VerQueryValueW(
                buf, r"\ ", ctypes.byref(ptr), ctypes.byref(length)
            )
            and ptr.value
        ):
            ffi = _VS_FIXEDFILEINFO.from_address(ptr.value)
            ver = (
                f"{ffi.dwFileVersionMS >> 16}."
                f"{ffi.dwFileVersionMS & 0xFFFF}."
                f"{ffi.dwFileVersionLS >> 16}."
                f"{ffi.dwFileVersionLS & 0xFFFF}"
            )

        # --- Translation table -> StringFileInfo
        trans_ptr = ctypes.c_void_p()
        trans_len = wintypes.UINT()
        lang_cp = "040904B0"
        if (
            ctypes.windll.version.VerQueryValueW(
                buf,
                r"\VarFileInfo\Translation",
                ctypes.byref(trans_ptr),
                ctypes.byref(trans_len),
            )
            and trans_ptr.value
            and trans_len.value >= 4
        ):
            lang = ctypes.cast(trans_ptr.value, ctypes.POINTER(wintypes.WORD))[0]
            cp = ctypes.cast(trans_ptr.value, ctypes.POINTER(wintypes.WORD))[1]
            lang_cp = f"{lang:04x}{cp:04x}"

        def query(field: str) -> str:
            sub = f"\\StringFileInfo\\{lang_cp}\\{field}"
            p = ctypes.c_void_p()
            ln = wintypes.UINT()
            if (
                ctypes.windll.version.VerQueryValueW(
                    buf, sub, ctypes.byref(p), ctypes.byref(ln)
                )
                and p.value
            ):
                return ctypes.wstring_at(p.value, ln.value).rstrip("\x00")
            return ""

        company = query("CompanyName")
        fver = query("FileVersion") or ver
        return company.strip(), fver.strip()
    except Exception:
        return "", ""


# --------------------------------------------------------------------------
# Registry helpers (never raise)
# --------------------------------------------------------------------------
def reg_read_values(
    hive: int, path: str, *, view: int | None = None
) -> list[tuple[str, str]]:
    """Read all REG_SZ/REG_EXPAND_SZ values from a key. Returns [] on any error."""
    out: list[tuple[str, str]] = []
    if not IS_WINDOWS or winreg is None:
        return out
    view = winreg.KEY_WOW64_64KEY if view is None else view
    try:
        key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view)
    except OSError:
        return out
    try:
        i = 0
        while True:
            try:
                name, data, _typ = winreg.EnumValue(key, i)
            except OSError:
                break
            if name and isinstance(data, str):
                out.append((name, data))
            i += 1
    finally:
        key.Close()
    return out


def reg_read_binary(hive: int, path: str, name: str) -> bytes | None:
    if not IS_WINDOWS or winreg is None:
        return None
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            key = winreg.OpenKey(hive, path, 0, winreg.KEY_READ | view)
        except OSError:
            continue
        try:
            data, typ = winreg.QueryValueEx(key, name)
            if typ == winreg.REG_BINARY and isinstance(data, bytes):
                return data
        except OSError:
            continue
        finally:
            key.Close()
    return None


def reg_write_binary(hive: int, path: str, name: str, data: bytes) -> None:
    if not IS_WINDOWS or winreg is None:
        raise OSError("registry unavailable")
    key = winreg.CreateKeyEx(hive, path, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, name, 0, winreg.REG_BINARY, data)
    finally:
        key.Close()


def reg_delete_value(hive: int, path: str, name: str) -> None:
    if not IS_WINDOWS or winreg is None:
        raise OSError("registry unavailable")
    key = winreg.OpenKey(hive, path, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.DeleteValue(key, name)
    finally:
        key.Close()


def reg_write_string(hive: int, path: str, name: str, value: str) -> None:
    if not IS_WINDOWS or winreg is None:
        raise OSError("registry unavailable")
    key = winreg.CreateKeyEx(hive, path, 0, winreg.KEY_SET_VALUE)
    try:
        winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    finally:
        key.Close()
