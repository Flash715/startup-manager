"""Aggregates all collectors into one scan."""

from __future__ import annotations

from collections.abc import Callable

from .collectors import (
    FolderCollector,
    RegistryCollector,
    ServiceCollector,
    TaskCollector,
)
from .models import ALL_SOURCES, StartupEntry
from .winapi import IS_WINDOWS

CollectorFactory = Callable[[], object]

_COLLECTORS: dict[str, list[CollectorFactory]] = {
    "registry-user-run": [RegistryCollector],
    "registry-user-runonce": [RegistryCollector],
    "registry-machine-run": [RegistryCollector],
    "registry-machine-runonce": [RegistryCollector],
    "registry-machine-run-wow64": [RegistryCollector],
    "registry-machine-runonce-wow64": [RegistryCollector],
    "startup-folder-user": [FolderCollector],
    "startup-folder-common": [FolderCollector],
    "scheduled-task": [TaskCollector],
    "service": [ServiceCollector],
}


class Scanner:
    """Runs every collector and returns a merged, de-duplicated list."""

    def __init__(
        self, sources: list[str] | None = None, *, errors: list[str] | None = None
    ):
        self.sources = sources or ALL_SOURCES
        self.errors: list[str] = errors if errors is not None else []

    def scan(self) -> list[StartupEntry]:
        if not IS_WINDOWS:
            self.errors.append("not running on Windows — no entries can be read")
            return []

        entries: list[StartupEntry] = []
        wanted = set(self.sources)
        # Instantiate each collector once even if it serves several sources.
        instances: dict[object, object] = {}
        for source in self.sources:
            if source not in _COLLECTORS:
                self.errors.append(f"unknown source: {source}")
                continue
            for factory in _COLLECTORS[source]:
                if factory not in instances:
                    try:
                        instances[factory] = factory()
                    except Exception as exc:  # noqa: BLE001
                        self.errors.append(f"{factory.__name__} init failed: {exc}")
                        instances[factory] = None
                inst = instances[factory]
                if inst is None:
                    continue
                try:
                    found = inst.collect()  # type: ignore[attr-defined]
                except Exception as exc:  # noqa: BLE001
                    self.errors.append(f"{factory.__name__}.collect failed: {exc}")
                    continue
                for e in found:
                    if e.source in wanted:
                        entries.append(e)

        # De-duplicate: same id can be produced twice if two collectors overlap.
        seen: set[str] = set()
        unique: list[StartupEntry] = []
        for e in entries:
            if e.id in seen:
                continue
            seen.add(e.id)
            unique.append(e)

        unique.sort(key=lambda e: (not e.enabled, e.source, e.name.lower()))
        return unique


def filter_entries(
    entries: list[StartupEntry],
    *,
    sources: list[str] | None = None,
    scope: str | None = None,
    search: str | None = None,
    enabled_only: bool = False,
    disabled_only: bool = False,
    missing_only: bool = False,
) -> list[StartupEntry]:
    out = entries
    if sources:
        wanted = set(sources)
        out = [e for e in out if e.source in wanted]
    if scope:
        out = [e for e in out if e.scope == scope]
    if search:
        q = search.lower()
        out = [
            e
            for e in out
            if q in e.name.lower()
            or q in e.command.lower()
            or q in e.exe.lower()
            or q in e.publisher.lower()
        ]
    if enabled_only:
        out = [e for e in out if e.enabled]
    if disabled_only:
        out = [e for e in out if not e.enabled]
    if missing_only:
        out = [e for e in out if not e.exists]
    return out
