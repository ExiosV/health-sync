"""Minimal Google Drive helper: folders by path, spreadsheets by name (REST, no extra SDK)."""
from __future__ import annotations

import json

from google.auth.transport.requests import AuthorizedSession

from health_sync.connectors import google_auth

API = "https://www.googleapis.com/drive/v3"
FOLDER = "application/vnd.google-apps.folder"
SHEET = "application/vnd.google-apps.spreadsheet"


def _q(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


class Drive:
    def __init__(self, session: AuthorizedSession | None = None):
        self.s = session or AuthorizedSession(google_auth.credentials())

    def list(self, parent: str = "root", mime: str | None = None, name: str | None = None) -> list[dict]:
        q = [f"'{parent}' in parents", "trashed = false"]
        if mime:
            q.append(f"mimeType = '{mime}'")
        if name:
            q.append(f"name = '{_q(name)}'")
        r = self.s.get(f"{API}/files", params={"q": " and ".join(q), "fields": "files(id,name,mimeType)",
                                                "pageSize": 200, "orderBy": "name"})
        r.raise_for_status()
        return r.json()["files"]

    def _create(self, name: str, mime: str, parent: str) -> str:
        r = self.s.post(f"{API}/files", params={"fields": "id"},
                        json={"name": name, "mimeType": mime, "parents": [parent]})
        r.raise_for_status()
        return r.json()["id"]

    def ensure(self, name: str, mime: str, parent: str) -> str:
        found = self.list(parent, mime, name)
        return found[0]["id"] if found else self._create(name, mime, parent)

    def ensure_path(self, path: str) -> str:
        """'a/b/c' under My Drive; returns the id of the last folder. Existing folders are reused."""
        parent = "root"
        for part in filter(None, path.split("/")):
            parent = self.ensure(part, FOLDER, parent)
        return parent

    def ensure_spreadsheet(self, path: str, name: str) -> str:
        return self.ensure(name, SHEET, self.ensure_path(path))

    def upsert_file(self, parent: str, name: str, body: bytes, mime: str) -> str:
        """Create or replace file `name` in folder `parent`; returns the file id."""
        found = self.list(parent, name=name)
        if found:
            fid = found[0]["id"]
            r = self.s.patch(f"https://www.googleapis.com/upload/drive/v3/files/{fid}",
                             params={"uploadType": "media"}, data=body, headers={"Content-Type": mime})
        else:
            meta = json.dumps({"name": name, "parents": [parent], "mimeType": mime})
            boundary = "hs-boundary"
            payload = (f"--{boundary}\r\nContent-Type: application/json; charset=UTF-8\r\n\r\n{meta}\r\n"
                       f"--{boundary}\r\nContent-Type: {mime}\r\n\r\n").encode() + body + f"\r\n--{boundary}--".encode()
            r = self.s.post("https://www.googleapis.com/upload/drive/v3/files",
                            params={"uploadType": "multipart", "fields": "id"}, data=payload,
                            headers={"Content-Type": f"multipart/related; boundary={boundary}"})
        r.raise_for_status()
        return r.json()["id"]

    def upsert_json(self, parent: str, name: str, data: object) -> str:
        return self.upsert_file(parent, name, json.dumps(data, ensure_ascii=False).encode(), "application/json")
