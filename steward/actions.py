"""What the Ledger does once a maintainer approves a proposal, and how to undo it.

This module is imported only by the Ledger. Every action records what it did
on GitHub so `undo` can reverse exactly that, and nothing more.
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from typing import Any, Protocol

from . import db

FOOTER = "\n\n<sub>Drafted by Steward Crew (AI) and approved by a maintainer.</sub>"


class GitHubWriter(Protocol):
    def add_assignee(self, number: int, login: str) -> None: ...
    def remove_assignee(self, number: int, login: str) -> None: ...
    def comment(self, number: int, body: str) -> int: ...
    def delete_comment(self, comment_id: int) -> None: ...
    def add_label(self, number: int, label: str) -> None: ...
    def remove_label(self, number: int, label: str) -> None: ...
    def set_state(self, number: int, state: str) -> None: ...


@dataclass
class Outcome:
    ok: bool
    summary: str
    simulated: bool = False
    undo: list[dict[str, Any]] = field(default_factory=list)


class _Recorder:
    """Stands in for GitHub on demo records: does nothing, remembers nothing."""

    def add_assignee(self, number, login): ...
    def remove_assignee(self, number, login): ...
    def comment(self, number, body): return 0
    def delete_comment(self, comment_id): ...
    def add_label(self, number, label): ...
    def remove_label(self, number, label): ...
    def set_state(self, number, state): ...


def execute(conn: sqlite3.Connection, gh: GitHubWriter | None, proposal: dict[str, Any], *, demo_mode: bool) -> Outcome:
    simulated = db.is_simulated(conn, proposal)
    if simulated and not demo_mode:
        return Outcome(False, "this proposal points at demo data and demo mode is off")
    if gh is None and not simulated:
        return Outcome(False, "the Ledger has no GitHub token, so it cannot act")
    target = proposal["target"]
    login = proposal.get("login") or ""
    text = (proposal.get("text") or "").strip()
    writer: GitHubWriter = _Recorder() if simulated else gh  # type: ignore[assignment]
    undo: list[dict[str, Any]] = []
    kind = proposal["kind"]

    def post(body: str) -> None:
        cid = writer.comment(target, body + FOOTER)
        if cid:
            undo.append({"op": "delete_comment", "id": cid})

    if kind == "assign":
        writer.add_assignee(target, login)
        undo.append({"op": "remove_assignee", "login": login})
        if text:
            post(text)
        issue = db.get_issue(conn, target) or {}
        assignees = sorted(set((issue.get("assignees") or []) + [login]))
        db.set_assignees(conn, target, assignees)
        db.add_claim(conn, target, login, db.now(), via="assigned", source="demo" if simulated else "github")
        db.set_claim_status(conn, target, login, "assigned")
        summary = f"assigned #{target} to @{login}"
    elif kind == "release":
        issue = db.get_issue(conn, target) or {}
        if login in (issue.get("assignees") or []):
            writer.remove_assignee(target, login)
            undo.append({"op": "add_assignee", "login": login})
            db.set_assignees(conn, target, [a for a in issue.get("assignees") or [] if a != login])
        post(text)
        db.set_claim_status(conn, target, login, "released")
        undo.append({"op": "reopen_claim", "login": login})
        summary = f"released @{login}'s claim on #{target} and left a note"
    elif kind == "comment":
        post(text)
        summary = f"commented on #{target}"
    elif kind == "label":
        writer.add_label(target, text)
        undo.append({"op": "remove_label", "label": text})
        summary = f"labelled #{target} '{text}'"
    elif kind == "review":
        post(text)
        summary = f"posted the review on PR #{target}"
    elif kind == "close":
        post(text)
        writer.set_state(target, "closed")
        undo.append({"op": "reopen"})
        conn.execute("UPDATE prs SET state = 'closed' WHERE number = ?", (target,))
        summary = f"closed PR #{target} with a note"
    else:
        return Outcome(False, f"unknown proposal kind {kind}")
    if simulated:
        summary += " (demo record, simulated, nothing sent to GitHub)"
    return Outcome(True, summary, simulated=simulated, undo=undo)


def reverse(conn: sqlite3.Connection, gh: GitHubWriter | None, proposal: dict[str, Any]) -> Outcome:
    steps = json.loads(proposal.get("undo") or "[]") if isinstance(proposal.get("undo"), str) else proposal.get("undo") or []
    simulated = db.is_simulated(conn, proposal)
    if gh is None and not simulated:
        return Outcome(False, "the Ledger has no GitHub token, so it cannot undo")
    writer: GitHubWriter = _Recorder() if simulated else gh  # type: ignore[assignment]
    target = proposal["target"]
    done = []
    for step in reversed(steps):
        op = step["op"]
        if op == "delete_comment":
            writer.delete_comment(int(step["id"]))
            done.append("removed the comment")
        elif op == "remove_assignee":
            writer.remove_assignee(target, step["login"])
            issue = db.get_issue(conn, target) or {}
            db.set_assignees(conn, target, [a for a in issue.get("assignees") or [] if a != step["login"]])
            db.set_claim_status(conn, target, step["login"], "open")
            done.append(f"unassigned @{step['login']}")
        elif op == "add_assignee":
            writer.add_assignee(target, step["login"])
            issue = db.get_issue(conn, target) or {}
            db.set_assignees(conn, target, sorted(set((issue.get("assignees") or []) + [step["login"]])))
            done.append(f"re-assigned @{step['login']}")
        elif op == "reopen_claim":
            db.set_claim_status(conn, target, step["login"], "open")
            done.append(f"restored @{step['login']}'s claim")
        elif op == "remove_label":
            writer.remove_label(target, step["label"])
            done.append(f"removed label '{step['label']}'")
        elif op == "reopen":
            writer.set_state(target, "open")
            conn.execute("UPDATE prs SET state = 'open' WHERE number = ?", (target,))
            done.append("reopened it")
    summary = ", ".join(done) or "nothing to undo"
    if simulated:
        summary += " (demo record, simulated)"
    return Outcome(True, summary, simulated=simulated)
