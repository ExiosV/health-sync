"""Per-pipeline cursor kept in a small JSON file."""
from __future__ import annotations

import json
from pathlib import Path


class State:
    def __init__(self, path: str | Path):
        self.path = Path(path)
        try:
            self._data = json.loads(self.path.read_text())
        except (FileNotFoundError, ValueError):
            self._data = {}

    def get(self, key: str) -> str | None:
        return self._data.get(key)

    def set(self, key: str, value: str) -> None:
        self._data[key] = value
        self.path.write_text(json.dumps(self._data, indent=2))
