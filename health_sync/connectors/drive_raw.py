"""Raw archive sink: one JSON file per day (`YYYY-MM/YYYY-MM-DD.json`) with the unprocessed source responses."""
from __future__ import annotations

import logging
from typing import Iterable

from health_sync.connectors.gdrive import Drive
from health_sync.core.model import Record

log = logging.getLogger(__name__)


class DriveRawSink:
    name = "drive-raw"

    def __init__(self, folder_id: str, kind: str = "daily"):
        self.folder_id, self.kind, self.drive = folder_id, kind, Drive()
        self._months: dict[str, str] = {}

    def write(self, records: Iterable[Record]) -> int:
        n = 0
        for r in records:
            if r.kind != self.kind:
                continue
            month = r.start[:7]
            if month not in self._months:
                self._months[month] = self.drive.ensure(month, "application/vnd.google-apps.folder", self.folder_id)
            self.drive.upsert_json(self._months[month], f"{r.start}.json", r.raw)
            n += 1
        log.info("%s: %d day files", self.name, n)
        return n
