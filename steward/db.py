"""Steward's memory: one SQLite file shared by the Ledger and the agents' CLI.

The agents can read everything here and can add proposals. Only the Ledger
changes a proposal's status or writes the audit log, because only the Ledger
holds a GitHub token that can act.
"""

from __future__ import annotations

import json
import re
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    number      INTEGER PRIMARY KEY,
    title       TEXT NOT NULL,
    body        TEXT DEFAULT '',
    labels      TEXT DEFAULT '[]',
    state       TEXT DEFAULT 'open',
    assignees   TEXT DEFAULT '[]',
    created_at  REAL,
    updated_at  REAL,
    url         TEXT DEFAULT '',
    source      TEXT DEFAULT 'github'
);
CREATE TABLE IF NOT EXISTS comments (
    id          TEXT PRIMARY KEY,
    issue       INTEGER NOT NULL,
    login       TEXT NOT NULL,
    body        TEXT DEFAULT '',
    created_at  REAL,
    source      TEXT DEFAULT 'github'
);
CREATE TABLE IF NOT EXISTS claims (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    issue           INTEGER NOT NULL,
    login           TEXT NOT NULL,
    claimed_at      REAL NOT NULL,
    last_activity   REAL NOT NULL,
    status          TEXT NOT NULL DEFAULT 'open',
    via             TEXT DEFAULT 'github',
    source          TEXT DEFAULT 'github',
    UNIQUE(issue, login)
);
CREATE TABLE IF NOT EXISTS prs (
    number          INTEGER PRIMARY KEY,
    title           TEXT NOT NULL,
    body            TEXT DEFAULT '',
    author          TEXT NOT NULL,
    state           TEXT DEFAULT 'open',
    created_at      REAL,
    updated_at      REAL,
    additions       INTEGER DEFAULT 0,
    deletions       INTEGER DEFAULT 0,
    files           TEXT DEFAULT '[]',
    url             TEXT DEFAULT '',
    source          TEXT DEFAULT 'github'
);
CREATE TABLE IF NOT EXISTS contributors (
    login               TEXT PRIMARY KEY,
    account_created_at  REAL,
    first_seen          REAL,
    merged_prs          INTEGER DEFAULT 0,
    source              TEXT DEFAULT 'github'
);
CREATE TABLE IF NOT EXISTS proposals (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    kind            TEXT NOT NULL,
    target          INTEGER NOT NULL,
    login           TEXT DEFAULT '',
    text            TEXT DEFAULT '',
    reason          TEXT DEFAULT '',
    proposed_by     TEXT DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    created_at      REAL NOT NULL,
    decided_at      REAL,
    decided_by      TEXT DEFAULT '',
    announced       INTEGER DEFAULT 0,
    result          TEXT DEFAULT '',
    undo            TEXT DEFAULT '',
    source          TEXT DEFAULT 'github'
);
CREATE TABLE IF NOT EXISTS audit (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          REAL NOT NULL,
    actor       TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT DEFAULT '',
    proposal_id INTEGER,
    detail      TEXT DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (
    key     TEXT PRIMARY KEY,
    value   TEXT
);
"""

PROPOSAL_KINDS = ("assign", "release", "comment", "label", "review", "close")
PROPOSAL_STATUSES = ("pending", "approved", "declined", "done", "undone", "failed")


def connect(path: Path | None = None) -> sqlite3.Connection:
    conn = sqlite3.connect(path or config.db_path(), timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


def _migrate(conn: sqlite3.Connection) -> None:
    """Bring a database made by an older version up to the current schema."""
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(proposals)")}
    if "source" not in columns:
        conn.execute("ALTER TABLE proposals ADD COLUMN source TEXT DEFAULT 'github'")
        conn.commit()


@contextmanager
def session(path: Path | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now() -> float:
    return time.time()


def row_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    out = dict(row)
    for key in ("labels", "assignees", "files"):
        if key in out and isinstance(out[key], str):
            try:
                out[key] = json.loads(out[key])
            except json.JSONDecodeError:
                out[key] = []
    return out


# ── Meta ──────────────────────────────────────────────────────────────────────


def get_meta(conn: sqlite3.Connection, key: str, default: str | None = None) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


# ── Proposals ─────────────────────────────────────────────────────────────────


def add_proposal(
    conn: sqlite3.Connection,
    kind: str,
    target: int,
    *,
    login: str = "",
    text: str = "",
    reason: str = "",
    proposed_by: str = "",
) -> int:
    if kind not in PROPOSAL_KINDS:
        raise ValueError(f"unknown proposal kind {kind!r}; use one of {', '.join(PROPOSAL_KINDS)}")
    login = login.lstrip("@")
    same = "kind = ? AND target = ? AND login = ? AND status = 'pending'"
    params: list[Any] = [kind, target, login]
    if kind in ("comment", "review", "close"):
        # Two different notes on the same issue are two proposals.
        same += " AND text = ?"
        params.append(text)
    duplicate = conn.execute(f"SELECT id FROM proposals WHERE {same}", params).fetchone()
    if duplicate:
        return int(duplicate["id"])
    source = "demo" if touches_demo(conn, target, login=login, text=text, reason=reason) else "github"
    cur = conn.execute(
        "INSERT INTO proposals(kind, target, login, text, reason, proposed_by, status, created_at, source)"
        " VALUES(?, ?, ?, ?, ?, ?, 'pending', ?, ?)",
        (kind, target, login, text, reason, proposed_by, now(), source),
    )
    return int(cur.lastrowid)


def get_proposal(conn: sqlite3.Connection, pid: int) -> dict[str, Any] | None:
    return row_dict(conn.execute("SELECT * FROM proposals WHERE id = ?", (pid,)).fetchone())


def pending_proposals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM proposals WHERE status = 'pending' ORDER BY id").fetchall()
    return [row_dict(r) for r in rows]


def unannounced_proposals(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM proposals WHERE status = 'pending' AND announced = 0 ORDER BY id"
    ).fetchall()
    return [row_dict(r) for r in rows]


def mark_announced(conn: sqlite3.Connection, pid: int) -> None:
    conn.execute("UPDATE proposals SET announced = 1 WHERE id = ?", (pid,))


def set_proposal_status(
    conn: sqlite3.Connection,
    pid: int,
    status: str,
    *,
    decided_by: str = "",
    result: str = "",
    undo: dict[str, Any] | None = None,
) -> None:
    if status not in PROPOSAL_STATUSES:
        raise ValueError(status)
    conn.execute(
        "UPDATE proposals SET status = ?, decided_at = ?, decided_by = COALESCE(NULLIF(?, ''), decided_by),"
        " result = ?, undo = COALESCE(?, undo) WHERE id = ?",
        (status, now(), decided_by, result, json.dumps(undo) if undo is not None else None, pid),
    )


def set_proposal_text(conn: sqlite3.Connection, pid: int, text: str) -> None:
    conn.execute("UPDATE proposals SET text = ? WHERE id = ?", (text, pid))


# ── Audit ─────────────────────────────────────────────────────────────────────


def audit(
    conn: sqlite3.Connection,
    actor: str,
    action: str,
    target: str = "",
    *,
    proposal_id: int | None = None,
    detail: str = "",
) -> None:
    conn.execute(
        "INSERT INTO audit(at, actor, action, target, proposal_id, detail) VALUES(?, ?, ?, ?, ?, ?)",
        (now(), actor, action, target, proposal_id, detail),
    )


def recent_audit(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


# ── Issues, claims, pull requests ─────────────────────────────────────────────


def upsert_issue(conn: sqlite3.Connection, issue: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO issues(number, title, body, labels, state, assignees, created_at, updated_at, url, source)"
        " VALUES(:number, :title, :body, :labels, :state, :assignees, :created_at, :updated_at, :url, :source)"
        " ON CONFLICT(number) DO UPDATE SET title = excluded.title, body = excluded.body,"
        " labels = excluded.labels, state = excluded.state, assignees = excluded.assignees,"
        " updated_at = excluded.updated_at, url = excluded.url",
        {
            "number": issue["number"],
            "title": issue["title"],
            "body": issue.get("body") or "",
            "labels": json.dumps(issue.get("labels", [])),
            "state": issue.get("state", "open"),
            "assignees": json.dumps(issue.get("assignees", [])),
            "created_at": issue.get("created_at"),
            "updated_at": issue.get("updated_at"),
            "url": issue.get("url", ""),
            "source": issue.get("source", "github"),
        },
    )


def get_issue(conn: sqlite3.Connection, number: int) -> dict[str, Any] | None:
    return row_dict(conn.execute("SELECT * FROM issues WHERE number = ?", (number,)).fetchone())


def set_assignees(conn: sqlite3.Connection, number: int, assignees: list[str]) -> None:
    conn.execute("UPDATE issues SET assignees = ? WHERE number = ?", (json.dumps(assignees), number))


def add_comment(conn: sqlite3.Connection, comment: dict[str, Any]) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO comments(id, issue, login, body, created_at, source) VALUES(?, ?, ?, ?, ?, ?)",
        (
            str(comment["id"]),
            comment["issue"],
            comment["login"],
            comment.get("body", ""),
            comment.get("created_at", now()),
            comment.get("source", "github"),
        ),
    )
    return cur.rowcount > 0


def add_claim(
    conn: sqlite3.Connection, issue: int, login: str, at: float, *, via: str = "github", source: str = "github"
) -> bool:
    cur = conn.execute(
        "INSERT OR IGNORE INTO claims(issue, login, claimed_at, last_activity, status, via, source)"
        " VALUES(?, ?, ?, ?, 'open', ?, ?)",
        (issue, login.lstrip("@"), at, at, via, source),
    )
    return cur.rowcount > 0


def touch_claim(conn: sqlite3.Connection, issue: int, login: str, at: float) -> None:
    conn.execute(
        "UPDATE claims SET last_activity = MAX(last_activity, ?) WHERE issue = ? AND login = ?",
        (at, issue, login.lstrip("@")),
    )


def set_claim_status(conn: sqlite3.Connection, issue: int, login: str, status: str) -> None:
    conn.execute(
        "UPDATE claims SET status = ? WHERE issue = ? AND login = ?", (status, issue, login.lstrip("@"))
    )


def claims(conn: sqlite3.Connection, *, issue: int | None = None, active_only: bool = True) -> list[dict[str, Any]]:
    sql = "SELECT * FROM claims"
    where, args = [], []
    if issue is not None:
        where.append("issue = ?")
        args.append(issue)
    if active_only:
        where.append("status IN ('open', 'assigned')")
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY issue, claimed_at"
    return [dict(r) for r in conn.execute(sql, args).fetchall()]


def upsert_pr(conn: sqlite3.Connection, pr: dict[str, Any]) -> None:
    conn.execute(
        "INSERT INTO prs(number, title, body, author, state, created_at, updated_at, additions, deletions, files, url, source)"
        " VALUES(:number, :title, :body, :author, :state, :created_at, :updated_at, :additions, :deletions, :files, :url, :source)"
        " ON CONFLICT(number) DO UPDATE SET title = excluded.title, body = excluded.body, state = excluded.state,"
        " updated_at = excluded.updated_at, additions = excluded.additions, deletions = excluded.deletions,"
        " files = excluded.files, url = excluded.url",
        {
            "number": pr["number"],
            "title": pr["title"],
            "body": pr.get("body") or "",
            "author": pr["author"],
            "state": pr.get("state", "open"),
            "created_at": pr.get("created_at"),
            "updated_at": pr.get("updated_at"),
            "additions": pr.get("additions", 0),
            "deletions": pr.get("deletions", 0),
            "files": json.dumps(pr.get("files", [])),
            "url": pr.get("url", ""),
            "source": pr.get("source", "github"),
        },
    )


def get_pr(conn: sqlite3.Connection, number: int) -> dict[str, Any] | None:
    return row_dict(conn.execute("SELECT * FROM prs WHERE number = ?", (number,)).fetchone())


def open_prs(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [row_dict(r) for r in conn.execute("SELECT * FROM prs WHERE state = 'open' ORDER BY number").fetchall()]


def upsert_contributor(conn: sqlite3.Connection, login: str, **fields: Any) -> None:
    login = login.lstrip("@")
    conn.execute(
        "INSERT INTO contributors(login, account_created_at, first_seen, merged_prs, source) VALUES(?, ?, ?, ?, ?)"
        " ON CONFLICT(login) DO NOTHING",
        (
            login,
            fields.get("account_created_at"),
            fields.get("first_seen", now()),
            fields.get("merged_prs", 0),
            fields.get("source", "github"),
        ),
    )
    for key in ("account_created_at", "merged_prs"):
        if fields.get(key) is not None:
            conn.execute(f"UPDATE contributors SET {key} = ? WHERE login = ?", (fields[key], login))


def get_contributor(conn: sqlite3.Connection, login: str) -> dict[str, Any] | None:
    return row_dict(conn.execute("SELECT * FROM contributors WHERE login = ?", (login.lstrip("@"),)).fetchone())


def demo_logins(conn: sqlite3.Connection) -> set[str]:
    """Every username the demo replay made up."""
    names: set[str] = set()
    for table in ("contributors", "claims", "comments"):
        names.update(r[0].lower() for r in conn.execute(f"SELECT DISTINCT login FROM {table} WHERE source = 'demo'"))
    return names


def touches_demo(conn: sqlite3.Connection, target: int, *, login: str = "", text: str = "", reason: str = "") -> bool:
    """True when a proposal is about something the demo replay made up.

    That is: a demo issue or pull request, a demo person as the subject, or a
    demo person named anywhere in the note or the reason. The last one matters:
    a comment on a real issue that says "@asha-dev, thanks" must never reach
    GitHub, where that name may belong to a stranger.
    """
    for table in ("prs", "issues"):
        row = conn.execute(f"SELECT source FROM {table} WHERE number = ?", (target,)).fetchone()
        if row and row["source"] == "demo":
            return True
    names = demo_logins(conn)
    if not names:
        return False
    if login.lstrip("@").lower() in names:
        return True
    words = {w.lower() for w in re.findall(r"[A-Za-z0-9](?:[A-Za-z0-9-]*[A-Za-z0-9])?", f"{text}\n{reason}")}
    return bool(words & names)


def is_simulated(conn: sqlite3.Connection, proposal: dict[str, Any]) -> bool:
    """True when the Ledger must simulate a proposal instead of sending it to GitHub.

    Decided when the proposal was filed and checked again when it is approved,
    so removing the demo data later never turns a demo proposal into a real one.
    """
    if proposal.get("source") == "demo":
        return True
    return touches_demo(
        conn,
        proposal["target"],
        login=proposal.get("login") or "",
        text=proposal.get("text") or "",
        reason=proposal.get("reason") or "",
    )
