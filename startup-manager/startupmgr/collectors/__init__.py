"""Collectors for each Windows autostart source."""

from .folder import FolderCollector
from .registry import RegistryCollector
from .services import ServiceCollector
from .tasks import TaskCollector

__all__ = [
    "RegistryCollector",
    "FolderCollector",
    "TaskCollector",
    "ServiceCollector",
]
