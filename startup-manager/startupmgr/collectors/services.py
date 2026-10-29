"""Windows service collector (auto-start services only).

Services are *never* deleted by this tool — at most disabled, and only with an
explicit opt-in flag. Disabling the wrong service can make Windows unbootable.
"""

from __future__ import annotations

import os
import subprocess

from ..models import StartupEntry
from ..winapi import IS_WINDOWS, file_version_info, split_command

_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

# Services that must never be touched, even with --i-know-what-im-doing.
PROTECTED = {
    "RpcSs",
    "RpcEptMapper",
    "DcomLaunch",
    "LSM",
    "Power",
    "PlugPlay",
    "BFE",
    "MpsSvc",
    "WinDefend",
    "CryptSvc",
    "EventLog",
    "Schedule",
    "ProfSvc",
    "UserManager",
    "SamSs",
    "gpsvc",
    "LanmanServer",
    "LanmanWorkstation",
    "Themes",
    "AudioSrv",
    "Audiosrv",
    "Dhcp",
    "Dnscache",
    "nsi",
    "TrustedInstaller",
    "Winmgmt",
    "Wcmsvc",
    "TextInputHost",
    "StateRepository",
    "CoreMessagingRegistrar",
}


def _ps(script: str, timeout: int = 40) -> str:
    try:
        res = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                script,
            ],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
            errors="replace",
        )
        return res.stdout or ""
    except Exception:
        return ""


# Emit "Name|DisplayName|StartMode|PathName" per line — robust against JSON quirks.
PS_QUERY = (
    "Get-CimInstance Win32_Service -Filter \"StartMode='Auto'\" | "
    "ForEach-Object { "
    '"$($_.Name)|$($_.DisplayName)|$($_.StartMode)|$($_.PathName)" }'
)


class ServiceCollector:
    """Enumerates services with StartMode = Auto."""

    def collect(self) -> list[StartupEntry]:
        if not IS_WINDOWS:
            return []
        raw = _ps(PS_QUERY)
        if not raw.strip():
            raw = self._fallback_sc()

        entries: list[StartupEntry] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or "|" not in line:
                continue
            parts = line.split("|", 3)
            if len(parts) < 4:
                continue
            name, display, mode, pathname = parts
            name = name.strip()
            if not name:
                continue
            exe, args = split_command(pathname.strip())
            publisher, fver = file_version_info(exe)
            protected = name in PROTECTED
            entries.append(
                StartupEntry(
                    name=name,
                    source="service",
                    location=rf"HKLM\SYSTEM\CurrentControlSet\Services\{name}",
                    scope="machine",
                    command=pathname.strip(),
                    exe=exe,
                    args=args,
                    exists=bool(exe) and os.path.isfile(exe),
                    publisher=publisher,
                    file_version=fver,
                    enabled=mode.lower() != "disabled",
                    state_known=True,
                    # Deleting a service is never allowed; disabling needs the flag.
                    deletable=False,
                    delete_block_reason=(
                        "protected system service"
                        if protected
                        else "services are never deleted — use `disable --i-know-what-im-doing`"
                    ),
                    raw={"display_name": display.strip(), "protected": protected},
                )
            )
        entries.sort(key=lambda e: e.name.lower())
        return entries

    def _fallback_sc(self) -> str:
        out = _ps(
            "Get-Service | Where-Object {$_.StartType -eq 'Automatic'} | "
            'ForEach-Object { "$($_.Name)|$($_.DisplayName)|Auto|" }'
        )
        return out

    # -- mutations -------------------------------------------------------
    def set_enabled(self, entry: StartupEntry, enabled: bool) -> None:
        if not enabled and entry.raw.get("protected"):
            raise PermissionError(
                f"{entry.name} is a protected core service and cannot be disabled"
            )
        mode = "auto" if enabled else "disabled"
        res = subprocess.run(
            ["sc", "config", entry.name, "start=", mode],
            capture_output=True,
            text=True,
            timeout=20,
            creationflags=_NO_WINDOW,
            errors="replace",
        )
        blob = (res.stdout or "") + (res.stderr or "")
        if res.returncode != 0 or "denied" in blob.lower():
            raise PermissionError(blob.strip()[:300])

    def delete(self, entry: StartupEntry) -> None:
        raise PermissionError(
            "Deleting services is intentionally unsupported. "
            "Disable it instead: startupmgr disable <id> --i-know-what-im-doing"
        )
