"""Data models for startup entries."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any, Literal

Scope = Literal["user", "machine"]


@dataclass
class StartupEntry:
    """A single autostart item discovered on the system."""

    name: str
    source: str
    location: str
    scope: Scope = "user"
    command: str = ""
    exe: str = ""
    args: str = ""
    exists: bool = False
    publisher: str = ""
    file_version: str = ""
    enabled: bool = True
    state_known: bool = True
    deletable: bool = True
    delete_block_reason: str = ""
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def id(self) -> str:
        """Stable short id derived from the identity triple."""
        key = f"{self.source}|{self.name}|{self.location}".lower()
        return hashlib.sha1(key.encode("utf-8", "replace")).hexdigest()[:10]

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("raw", None)
        d["id"] = self.id
        return d

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StartupEntry:
        d = dict(d)
        d.pop("id", None)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in d.items() if k in known})


# Human-readable labels for every source id.
SOURCE_LABELS: dict[str, str] = {
    "registry-user-run": "HKCU Run",
    "registry-user-runonce": "HKCU RunOnce",
    "registry-machine-run": "HKLM Run",
    "registry-machine-runonce": "HKLM RunOnce",
    "registry-machine-run-wow64": "HKLM Run (32-bit)",
    "registry-machine-runonce-wow64": "HKLM RunOnce (32-bit)",
    "startup-folder-user": "Startup Folder (User)",
    "startup-folder-common": "Startup Folder (All Users)",
    "scheduled-task": "Scheduled Task",
    "service": "Service",
}

ALL_SOURCES: list[str] = list(SOURCE_LABELS)
