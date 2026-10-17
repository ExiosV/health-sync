"""Entry point: `python -m health_sync run <pipeline>`."""
from __future__ import annotations

import argparse
import logging
import os

from dotenv import load_dotenv

from health_sync.connectors.drive_digest import DriveDigestSink
from health_sync.connectors.drive_raw import DriveRawSink
from health_sync.connectors.garmin import GarminSource
from health_sync.connectors.garmin_health import GarminHealthSource
from health_sync.connectors.gsheets import SheetsSink
from health_sync.core.state import State

# Pipelines are code for now; they become YAML once there is a second user. Each is (source, [sinks]).
PIPELINES = {
    "garmin-activities-to-sheet": lambda: (GarminSource(), [SheetsSink("activity")]),
    "garmin-health-to-drive": lambda: (GarminHealthSource(), [
        SheetsSink("daily", os.environ["HEALTH_SPREADSHEET_ID"], "Дни"),
        SheetsSink("blood_pressure", os.environ["HEALTH_SPREADSHEET_ID"], "Давление"),
        SheetsSink("weight", os.environ["HEALTH_SPREADSHEET_ID"], "Вес"),
        DriveRawSink(os.environ["HEALTH_RAW_FOLDER_ID"]),
        DriveDigestSink(os.environ["HEALTH_DIGEST_FOLDER_ID"], os.environ["HEALTH_SPREADSHEET_ID"]),  # last: reads the sheets
    ]),
}


def run(name: str, state_path: str) -> None:
    source, sinks = PIPELINES[name]()
    state = State(state_path)
    records = sorted(source.fetch(since=state.get(name)), key=lambda r: r.start, reverse=True)  # newest first
    for sink in sinks:
        sink.write(records)
    if records:
        state.set(name, records[0].start)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(prog="health-sync")
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("run", help="run one pipeline")
    p.add_argument("pipeline", choices=sorted(PIPELINES))
    p.add_argument("--state", default=os.getenv("HEALTH_SYNC_STATE", ".health_sync_state.json"))
    i = sub.add_parser("init-sheet", help="create (or find) a spreadsheet in a Drive folder path, print its id")
    i.add_argument("--path", required=True, help="folder path under My Drive, e.g. '04 .../спорт'")
    i.add_argument("--name", required=True)
    sub.add_parser("garmin-ticket-login", help="exchange the one-time ticket in GARMIN_TICKET for saved Garmin tokens")
    sub.add_parser("scale-ingest", help="serve the endpoint that receives Mi Scale measurements and uploads them to Garmin")
    ls = sub.add_parser("drive-ls", help="list sub-folders of a Drive folder path")
    ls.add_argument("--path", default="")
    args = parser.parse_args()
    if args.cmd == "scale-ingest":
        from health_sync.scale.ingest import serve
        serve()
        return
    if args.cmd == "garmin-ticket-login":
        from health_sync.connectors.garmin import login_with_ticket
        login_with_ticket(os.environ["GARMIN_TICKET"], os.getenv("GARMINTOKENS", "~/.garminconnect"))
        print("tokens saved")
        return
    if args.cmd in ("init-sheet", "drive-ls"):
        from health_sync.connectors.gdrive import FOLDER, Drive
        drive = Drive()
        if args.cmd == "init-sheet":
            print(drive.ensure_spreadsheet(args.path, args.name))
        else:
            parent = "root"
            for part in filter(None, args.path.split("/")):
                parent = drive.list(parent, FOLDER, part)[0]["id"]
            for f in drive.list(parent):
                print(("[dir] " if f["mimeType"] == FOLDER else "      ") + f["name"])
        return
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    run(args.pipeline, args.state)


if __name__ == "__main__":
    main()
