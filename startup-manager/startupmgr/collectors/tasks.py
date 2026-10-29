"""Scheduled-task collector (logon / boot triggers only)."""

from __future__ import annotations

import csv
import io
import json
import os
import subprocess
from typing import Any

from ..models import StartupEntry
from ..winapi import IS_WINDOWS, file_version_info, split_command

_NO_WINDOW = 0x08000000 if IS_WINDOWS else 0

# One PowerShell pass -> JSON. "-Depth 3" keeps nested trigger class names intact.
PS_QUERY = (
    "Get-ScheduledTask -ErrorAction SilentlyContinue | ForEach-Object {"
    "  $t = $_;"
    "  $trigs = @();"
    "  foreach ($tr in $t.Triggers) { $trigs += $tr.CimClass.CimClassName };"
    "  $acts = @();"
    '  foreach ($a in $t.Actions) { $acts += ("$($a.Execute) $($a.Arguments)").Trim() };'
    "  [pscustomobject]@{"
    "    TaskName = $t.TaskName;"
    "    TaskPath = $t.TaskPath;"
    "    State    = [string]$t.State;"
    "    Author   = $t.Author;"
    "    Triggers = $trigs;"
    "    Actions  = $acts"
    "  }"
    "} | ConvertTo-Json -Depth 3 -Compress"
)


def _run(cmd: list[str], timeout: int = 30) -> str:
    try:
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=_NO_WINDOW,
            errors="replace",
        )
        out = res.stdout or ""
        if res.returncode != 0 and res.stderr:
            out += "\n" + res.stderr
        return out
    except Exception:
        return ""


def _powershell_json(script: str) -> Any:
    out = _run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            script,
        ]
    )
    out = out.strip()
    if not out:
        return None
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        # A BOM or warning line sometimes sneaks in; retry from the first bracket.
        for i, ch in enumerate(out):
            if ch in "[{":
                try:
                    return json.loads(out[i:])
                except json.JSONDecodeError:
                    break
    return None


class TaskCollector:
    """Finds scheduled tasks that fire at logon or boot."""

    def collect(self) -> list[StartupEntry]:
        if not IS_WINDOWS:
            return []
        data = _powershell_json(PS_QUERY)
        if isinstance(data, dict):
            data = [data]
        if not isinstance(data, list) or not data:
            return self._collect_fallback()

        entries: list[StartupEntry] = []
        for t in data:
            if not isinstance(t, dict):
                continue
            triggers = [str(x) for x in (t.get("Triggers") or [])]
            if not any("LogonTrigger" in tr or "BootTrigger" in tr for tr in triggers):
                continue

            name = str(t.get("TaskName") or "")
            if not name:
                continue
            path = str(t.get("TaskPath") or "\\")
            state = str(t.get("State") or "")
            actions = t.get("Actions") or []
            if isinstance(actions, str):
                actions = [actions]
            action = " ; ".join(str(a) for a in actions if a and str(a).strip()).strip()
            if not action:
                action = ""  # COM-handler task: no executable to launch

            exe, args = split_command(action)
            publisher, fver = file_version_info(exe)
            entries.append(
                StartupEntry(
                    name=name,
                    source="scheduled-task",
                    location=path,
                    scope="machine",
                    command=action,
                    exe=exe,
                    args=args,
                    exists=bool(exe) and os.path.isfile(exe),
                    publisher=publisher or str(t.get("Author") or ""),
                    file_version=fver,
                    enabled=not state.lower().startswith("disabled"),
                    state_known=True,
                    deletable=True,
                    raw={"task_path": path, "task_name": name},
                )
            )
        return entries

    def _collect_fallback(self) -> list[StartupEntry]:
        """Parse ``schtasks /query /v /fo CSV`` when PowerShell is unavailable."""
        out = _run(["schtasks", "/query", "/fo", "CSV", "/nh", "/v"])
        if not out:
            return []
        entries: list[StartupEntry] = []
        reader = csv.reader(io.StringIO(out))
        headers: list[str] | None = None
        for row in reader:
            if not row or not any(c.strip() for c in row):
                continue
            if headers is None:
                headers = [c.strip() for c in row]
                continue
            rec = dict(zip(headers, row, strict=False))
            schedule = " ".join(
                v for k, v in rec.items() if "Schedule" in k or "Jadwal" in k
            ).lower()
            if "logon" not in schedule and "boot" not in schedule:
                continue
            task_name = (rec.get("TaskName") or "").strip()
            if not task_name:
                continue
            action = rec.get("Task To Run") or ""
            status = " ".join(
                v for k, v in rec.items() if k in ("Status", "Ready", "Siap")
            ).lower()
            exe, args = split_command(action)
            full = task_name.lstrip("\\")
            leaf = full.rsplit("\\", 1)[-1]
            parent = "\\" + full.rsplit("\\", 1)[0] if "\\" in full else "\\"
            entries.append(
                StartupEntry(
                    name=leaf,
                    source="scheduled-task",
                    location=parent,
                    scope="machine",
                    command=action,
                    exe=exe,
                    args=args,
                    exists=bool(exe) and os.path.isfile(exe),
                    enabled="disabled" not in status,
                    deletable=True,
                    raw={"task_path": parent, "task_name": full},
                )
            )
        return entries

    # -- mutations -------------------------------------------------------
    def set_enabled(self, entry: StartupEntry, enabled: bool) -> None:
        full = entry.location.rstrip("\\") + "\\" + entry.name
        flag = "/enable" if enabled else "/disable"
        _check(_run(["schtasks", "/change", "/tn", full, flag]), flag)

    def delete(self, entry: StartupEntry) -> None:
        full = entry.location.rstrip("\\") + "\\" + entry.name
        # Stash the XML so restore can re-create the task exactly.
        entry.raw["xml"] = _run(["schtasks", "/query", "/tn", full, "/xml", "ONE"])
        _check(_run(["schtasks", "/delete", "/tn", full, "/f"]), "delete")


def _check(output: str, what: str) -> None:
    low = output.lower()
    if "error" in low or "denied" in low or "ditolak" in low or "cannot find" in low:
        raise PermissionError(f"schtasks {what} failed: {output.strip()[:300]}")
