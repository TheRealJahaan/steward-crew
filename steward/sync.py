"""Copy what is happening on GitHub into Steward's database. Read-only."""

from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any

from . import db, signals
from .github import GitHub, GitHubError

log = logging.getLogger("steward.sync")


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def sync(conn: sqlite3.Connection, gh: GitHub, *, self_login: str = "") -> dict[str, Any]:
    """One pass over the repository. Returns what was new, for the log."""
    report: dict[str, Any] = {"issues": 0, "new_claims": [], "released": [], "new_prs": []}
    started = db.now()

    # Issues. Anything Steward thought was open but GitHub no longer lists is closed.
    open_issues = gh.issues()
    seen = set()
    for issue in open_issues:
        db.upsert_issue(conn, {**issue, "source": "github"})
        seen.add(issue["number"])
    report["issues"] = len(open_issues)
    for row in conn.execute("SELECT number FROM issues WHERE source = 'github' AND state = 'open'").fetchall():
        if row["number"] not in seen:
            conn.execute("UPDATE issues SET state = 'closed' WHERE number = ?", (row["number"],))
            conn.execute(
                "UPDATE claims SET status = 'done' WHERE issue = ? AND status IN ('open', 'assigned')",
                (row["number"],),
            )

    # Comments since the last pass: claims, releases, and activity on claims.
    since = db.get_meta(conn, "comments_since")
    for comment in gh.issue_comments(since):
        if comment["is_bot"] or (self_login and comment["login"].lower() == self_login.lower()):
            continue
        if not db.add_comment(conn, {**comment, "source": "github"}):
            continue
        number, login, at = comment["issue"], comment["login"], comment["created_at"] or db.now()
        db.upsert_contributor(conn, login, first_seen=at)
        if signals.is_release(comment["body"]):
            db.set_claim_status(conn, number, login, "released")
            report["released"].append((number, login))
        elif signals.is_claim(comment["body"]):
            if db.add_claim(conn, number, login, at):
                report["new_claims"].append((number, login))
            else:
                db.touch_claim(conn, number, login, at)
        else:
            db.touch_claim(conn, number, login, at)
    db.set_meta(conn, "comments_since", _iso(started - 120))

    # Assignees: an assigned claim is a claim a maintainer accepted.
    for issue in open_issues:
        for login in issue["assignees"]:
            if not db.add_claim(conn, issue["number"], login, issue["updated_at"] or db.now(), via="assigned"):
                pass
            conn.execute(
                "UPDATE claims SET status = 'assigned' WHERE issue = ? AND login = ? AND status = 'open'",
                (issue["number"], login),
            )

    # Pull requests: details for new or changed ones, and the activity they add to claims.
    for pr in gh.pulls("open"):
        known = db.get_pr(conn, pr["number"])
        files = known["files"] if known else []
        if not known or (known.get("updated_at") or 0) < (pr["updated_at"] or 0) or not files:
            try:
                files = gh.pull_files(pr["number"])
            except GitHubError as err:
                log.warning("could not read files of PR #%s: %s", pr["number"], err)
        db.upsert_pr(
            conn,
            {
                **pr,
                "files": files,
                "additions": sum(f["additions"] for f in files),
                "deletions": sum(f["deletions"] for f in files),
                "source": "github",
            },
        )
        if not known:
            report["new_prs"].append(pr["number"])
        if not db.get_contributor(conn, pr["author"]) or not (db.get_contributor(conn, pr["author"]) or {}).get(
            "account_created_at"
        ):
            db.upsert_contributor(
                conn, pr["author"], account_created_at=gh.user_created_at(pr["author"]), first_seen=pr["created_at"]
            )
        for number in signals.linked_issues(f"{pr['title']}\n{pr['body']}"):
            db.touch_claim(conn, number, pr["author"], pr["updated_at"] or db.now())

    # Merged pull requests per author, so newcomers can be told apart.
    merged: dict[str, int] = {}
    for pr in gh.pulls("closed"):
        if pr["state"] == "merged":
            merged[pr["author"]] = merged.get(pr["author"], 0) + 1
        conn.execute("UPDATE prs SET state = ? WHERE number = ? AND source = 'github'", (pr["state"], pr["number"]))
    for login, count in merged.items():
        db.upsert_contributor(conn, login, merged_prs=count)

    db.set_meta(conn, "last_sync", json.dumps({"at": started, "issues": report["issues"]}))
    return report
