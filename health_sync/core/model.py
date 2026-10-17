"""Canonical record that every connector reads or writes."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class Record:
    kind: str                 # activity, sleep, weight, ...
    source: str               # connector name that produced it
    external_id: str          # id in the source system
    start: str                # ISO 8601 start time
    payload: dict[str, Any] = field(default_factory=dict)  # normalized fields
    raw: dict[str, Any] = field(default_factory=dict)      # source response as is

    @property
    def key(self) -> tuple[str, str, str]:
        """Dedup key."""
        return (self.source, self.kind, self.external_id)
