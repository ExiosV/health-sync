"""Digest sink: rebuilds `YYYY-MM.md` in a Drive folder from the Sheets after they were updated."""
from __future__ import annotations

import logging
import os
from datetime import date
from typing import Iterable

import gspread

from health_sync.connectors import google_auth
from health_sync.connectors.gdrive import Drive
from health_sync.core.model import Record
from health_sync.digest import build_month

log = logging.getLogger(__name__)


class DriveDigestSink:
    name = "drive-digest"

    def __init__(self, folder_id: str, health_sheet_id: str, activities_sheet_id: str | None = None):
        self.folder_id, self.health_sheet_id = folder_id, health_sheet_id
        self.activities_sheet_id = activities_sheet_id or os.getenv("SPREADSHEET_ID")

    def write(self, records: Iterable[Record]) -> int:
        months = {r.start[:7] for r in records} or {date.today().isoformat()[:7]}
        gc = gspread.authorize(google_auth.credentials())
        health = gc.open_by_key(self.health_sheet_id)
        tab = lambda name: health.worksheet(name).get_all_records()  # noqa: E731
        daily, bp, weight = tab("Дни"), tab("Давление"), tab("Вес")
        acts = gc.open_by_key(self.activities_sheet_id).sheet1.get_all_records() if self.activities_sheet_id else []
        drive = Drive()
        for month in sorted(months):
            drive.upsert_file(self.folder_id, f"{month}.md", build_month(month, daily, bp, weight, acts).encode(), "text/markdown")
        log.info("drive-digest: %d month files", len(months))
        return len(months)
