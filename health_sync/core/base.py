"""Connector contracts. A service may implement either or both."""
from __future__ import annotations

from typing import Iterable, Protocol

from health_sync.core.model import Record


class Source(Protocol):
    name: str

    def fetch(self, since: str | None = None) -> Iterable[Record]: ...


class Sink(Protocol):
    name: str

    def write(self, records: Iterable[Record]) -> int:
        """Write records, skipping ones already present. Returns how many were added."""
        ...
