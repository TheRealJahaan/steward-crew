"""Replay a busy week of contributors into Steward's database, for demos.

Every record this writes is marked `source = 'demo'`. The Ledger treats any
proposal that touches one as simulated and never sends it to GitHub, so a demo
can run against a real repository without bothering anyone.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import db

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "demo" / "flood.json"


def _say(quiet: bool, text: str) -> None:
    if not quiet:
        print(f"{datetime.now():%H:%M:%S}  {text}", flush=True)


def _claim(conn: sqlite3.Connection, ev: dict, at: float, *, last_activity: float | None = None) -> None:
    db.add_comment(
        conn,
        {"id": f"demo-{uuid.uuid4().hex[:10]}", "issue": ev["issue"], "login": ev["login"], "body": ev.get("text", ""), "created_at": at, "source": "demo"},
    )
    db.add_claim(conn, ev["issue"], ev["login"], at, via="comment", source="demo")
    if last_activity is not None:
        conn.execute(
            "UPDATE claims SET last_activity = ? WHERE issue = ? AND login = ?", (last_activity, ev["issue"], ev["login"])
        )


def play(conn: sqlite3.Connection, *, path: Path | None = None, speed: float = 1.0, quiet: bool = False) -> int:
    data = json.loads((path or DEFAULT_FILE).read_text(encoding="utf-8"))
    now = db.now()
    for person in data.get("contributors", []):
        db.upsert_contributor(
            conn,
            person["login"],
            account_created_at=now - person.get("account_age_days", 365) * 86400,
            merged_prs=person.get("merged_prs", 0),
            first_seen=now,
            source="demo",
        )
    for ev in data.get("history", []):
        at = now - ev["hours_ago"] * 3600
        _claim(conn, ev, at, last_activity=now - ev.get("idle_hours", ev["hours_ago"]) * 3600)
        _say(quiet, f"(earlier) @{ev['login']} claimed #{ev['issue']}, no activity for {ev.get('idle_hours', ev['hours_ago'])}h")
    conn.commit()

    started = time.monotonic()
    for ev in sorted(data.get("events", []), key=lambda e: e["at"]):
        if speed > 0:
            wait = ev["at"] / speed - (time.monotonic() - started)
            if wait > 0:
                time.sleep(wait)
        at = db.now()
        kind = ev["type"]
        if kind == "claim":
            _claim(conn, ev, at)
            _say(quiet, f"@{ev['login']} on #{ev['issue']}: \"{ev['text']}\"")
        elif kind == "comment":
            db.add_comment(conn, {"id": f"demo-{uuid.uuid4().hex[:10]}", "issue": ev["issue"], "login": ev["login"], "body": ev["text"], "created_at": at, "source": "demo"})
            db.touch_claim(conn, ev["issue"], ev["login"], at)
            _say(quiet, f"@{ev['login']} on #{ev['issue']}: \"{ev['text']}\"")
        elif kind == "pr":
            files = ev.get("files", [])
            db.upsert_pr(
                conn,
                {
                    "number": ev["number"],
                    "title": ev["title"],
                    "body": ev.get("body", ""),
                    "author": ev["author"],
                    "state": "open",
                    "created_at": at,
                    "updated_at": at,
                    "additions": sum(f.get("additions", 0) for f in files),
                    "deletions": sum(f.get("deletions", 0) for f in files),
                    "files": files,
                    "url": "",
                    "source": "demo",
                },
            )
            from .signals import linked_issues

            for number in linked_issues(f"{ev['title']}\n{ev.get('body', '')}"):
                db.touch_claim(conn, number, ev["author"], at)
            _say(quiet, f"@{ev['author']} opened PR #{ev['number']}: {ev['title']}")
        conn.commit()
    _say(quiet, "Demo flood finished. Steward's agents can now see it with `steward status`.")
    return 0


def reset(conn: sqlite3.Connection) -> int:
    removed = 0
    for table in ("claims", "comments", "prs", "contributors"):
        removed += conn.execute(f"DELETE FROM {table} WHERE source = 'demo'").rowcount
    demo_numbers = [r[0] for r in conn.execute("SELECT number FROM issues WHERE source = 'demo'")]
    removed += conn.execute("DELETE FROM issues WHERE source = 'demo'").rowcount
    placeholders = ",".join("?" * len(demo_numbers)) or "NULL"
    removed += conn.execute(
        f"DELETE FROM proposals WHERE status = 'pending' AND (target >= 100 OR target IN ({placeholders}))",
        demo_numbers,
    ).rowcount
    return removed
